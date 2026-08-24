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
        speaker = client_name if m.direction == "in" else "Я"
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


async def _notify_admins_new_request(bot, telegram_id: int, full_name: str) -> None:
    payment = await database.get_payment_for_user(telegram_id)
    kb = keyboards.access_decision_kb(telegram_id, has_unrefunded_payment=payment is not None)
    text = f"🆕 Заявка на доступ\n\n{full_name} (id {telegram_id})"
    for admin in await database.list_admins_and_directors():
        try:
            await bot.send_message(admin.telegram_id, text, reply_markup=kb)
        except TelegramForbiddenError:
            await database.mark_user_unreachable(admin.telegram_id)


@registration_router.message(RegistrationStates.waiting_for_full_name, SafeFreeText())
async def process_full_name(message: Message, state: FSMContext) -> None:
    telegram_id = message.from_user.id
    full_name = message.text.strip()
    await database.create_or_update_user(telegram_id, message.from_user.username, full_name, message.from_user.last_name)
    await database.log_access_request(telegram_id, "requested", None, note=full_name)
    await state.clear()
    await message.answer("Заявка отправлена, ожидайте решения администратора.")
    await _notify_admins_new_request(message.bot, telegram_id, full_name)


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
    new_state = message.text == constants.BTN_SHIFT_ON
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

    point_id = int(parts[2])
    current = {p.id for p in await database.get_user_points(user_id)}
    if point_id in current:
        await database.unsubscribe_user_from_point(user_id, point_id)
        current.discard(point_id)
    else:
        await database.subscribe_user_to_point(user_id, point_id)
        current.add(point_id)
    all_points = await database.list_points()
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
    lines = ["📋 Ваши шаблоны:"]
    lines += [f"{'🧠' if t.kind == constants.TEMPLATE_AI_PROMPT else '📝'} {t.title}" for t in templates] or ["(пока нет)"]
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="➕ Текстовый шаблон", callback_data="tplnew_text"))
    builder.row(InlineKeyboardButton(text="➕ AI-промпт шаблон", callback_data="tplnew_ai_prompt"))
    await message.answer("\n".join(lines), reply_markup=builder.as_markup())


@menu_router.message(F.text == constants.BTN_ADMIN_PANEL, StateFilter("*"), RoleAtLeast(constants.ADMIN))
async def show_admin_panel(message: Message) -> None:
    await message.answer("⚙️ Настройки", reply_markup=keyboards.admin_panel_kb())


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
    await state.clear()

    msg_ref = await bot_cache.register_sent_message(chat.chat_id, last_avito_message_id)
    await anchor.answer(f"✅ Отправлено {sent_count} фото", reply_markup=keyboards.sent_message_kb(msg_ref))
    await _show_main_menu(anchor)


# --- templates ---------------------------------------------------------------


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
            draft, allow_send = template.body, True
        else:
            point = await database.get_point(chat.point_id) if chat.point_id else None
            if point is None:
                await callback.message.answer("У чата не определена точка, AI-шаблон недоступен.")
                return
            try:
                draft, flagged = await guardrail.guarded_generate(list(chat.messages), point, prompt_override=template.body)
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


@admin_router.callback_query(F.data == "adm_leadership")
async def cb_admin_leadership(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.answer("👔 Меню руководителя", reply_markup=keyboards.leadership_menu_kb())


@admin_router.callback_query(F.data == "adm_users")
async def cb_admin_users(callback: CallbackQuery) -> None:
    await callback.answer()
    users = await database.list_all_users()
    await callback.message.answer("👥 Все пользователи:", reply_markup=keyboards.user_management_kb(users))


@admin_router.callback_query(F.data.startswith("adm_useredit_"))
async def cb_admin_user_edit(callback: CallbackQuery) -> None:
    await callback.answer()
    target_id = int(callback.data.rsplit("_", 1)[1])
    actor = await database.get_user(callback.from_user.id)
    allow_admin_roles = bool(actor and actor.role == constants.DIRECTOR)
    await callback.message.answer("Выберите новую роль:", reply_markup=keyboards.role_select_kb(target_id, allow_admin_roles))


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


@admin_router.callback_query(F.data.startswith("adm_pointedit_"))
async def cb_admin_point_edit(callback: CallbackQuery) -> None:
    await callback.answer()
    point_id = int(callback.data.rsplit("_", 1)[1])
    point = await database.get_point(point_id)
    if point is None:
        return
    coords = await database.list_point_coordinates(point_id)
    lines = [f"🏢 {point.name}", f"Адрес: {point.address or '—'}", f"Часы: {point.working_hours or '—'}"]
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
