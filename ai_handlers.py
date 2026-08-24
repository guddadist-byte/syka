"""AI draft router: generate / edit / send / cancel.

Imports bot_cache and avito_client directly (same module-singleton
pattern as tasks.py/handlers.py) rather than through handlers.py.
"""

from __future__ import annotations

from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import ai_client
import avito_client
import bot_cache
import constants
import database
import guardrail
import keyboards
from filters import ApprovedUser, SafeFreeText
from states import AIStates

ai_router = Router(name="ai")
ai_router.message.filter(ApprovedUser())
ai_router.callback_query.filter(ApprovedUser())


@ai_router.callback_query(F.data.startswith(f"{constants.PREFIX_AIDRAFT}_"))
async def generate_ai_draft(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Генерирую черновик…")
    _, short_id = callback.data.split("_", 1)
    chat = await bot_cache.resolve_chat(short_id)
    if chat is None:
        return
    if chat.point_id is None:
        await callback.message.answer("У чата не определена точка, ИИ-ответ недоступен.")
        return
    point = await database.get_point(chat.point_id)
    if point is None:
        return
    try:
        draft, flagged = await guardrail.guarded_generate(list(chat.messages), point)
    except ai_client.AIClientError:
        await callback.message.answer("⚠️ Не удалось получить ответ от ИИ. Попробуйте позже.")
        return
    await state.update_data(chat_short_id=short_id, ai_draft=draft)
    await callback.message.answer(draft, reply_markup=keyboards.ai_draft_kb(short_id, allow_send=not flagged))


@ai_router.callback_query(F.data.startswith(f"{constants.PREFIX_AISEND}_"))
async def send_ai_draft(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    short_id = data.get("chat_short_id")
    draft = data.get("ai_draft")
    _, cb_short_id = callback.data.split("_", 1)
    if not short_id or short_id != cb_short_id or not draft:
        await callback.answer("Черновик устарел, сгенерируйте заново.", show_alert=True)
        return

    chat = await bot_cache.resolve_chat(short_id)
    if chat is None:
        await callback.answer("Чат не найден.", show_alert=True)
        return

    if not await bot_cache.try_claim_action(f"send:{chat.chat_id}"):
        await callback.answer("⏳ Уже отправляется…", show_alert=True)
        return

    client = avito_client.get_pool().get(chat.avito_account_id)
    if client is None:
        await callback.answer("Аккаунт Avito недоступен.", show_alert=True)
        return

    try:
        sent = await client.send_text_message(chat.chat_id, draft)
    except avito_client.AvitoAPIError:
        await callback.answer("Не удалось отправить сообщение.", show_alert=True)
        return

    await callback.answer()

    now = datetime.utcnow()
    await bot_cache.add_message(
        chat.chat_id,
        bot_cache.CachedMessage(
            avito_message_id=sent.message_id, direction="out", text=draft, has_image=False, created_at=now
        ),
    )
    await bot_cache.mark_replied(chat.chat_id, callback.from_user.id)
    await database.append_message(
        chat.chat_id, "out", draft, False,
        sent_at=now.strftime("%Y-%m-%d %H:%M:%S"), avito_message_id=sent.message_id,
    )
    await database.mark_chat_replied(chat.chat_id, callback.from_user.id)
    await database.increment_rating(callback.from_user.id)
    # unread_count is synced from Avito's own count on every poll (see
    # tasks.py), so this has to be mirrored to Avito or the next poll
    # would overwrite the local zero right back.
    try:
        await client.mark_chat_read(chat.chat_id)
    except avito_client.AvitoAPIError:
        pass

    msg_ref = await bot_cache.register_sent_message(chat.chat_id, sent.message_id or "")
    await state.clear()
    await callback.message.edit_text(f"✅ Отправлено:\n\n{draft}")
    await callback.message.answer("Готово.", reply_markup=keyboards.sent_message_kb(msg_ref))

    # callback.message is the bot's own message, not the actor's — resolve
    # the main menu keyboard from callback.from_user.id instead of reusing
    # handlers._show_main_menu (which would look up the wrong user).
    user = await database.get_user(callback.from_user.id)
    if user is not None:
        await callback.message.answer(
            "🏠 Главное меню", reply_markup=keyboards.main_menu_kb(bool(user.on_shift), user.role)
        )


@ai_router.callback_query(F.data.startswith(f"{constants.PREFIX_AIEDIT}_"))
async def edit_ai_draft(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    _, short_id = callback.data.split("_", 1)
    await state.update_data(chat_short_id=short_id)
    await state.set_state(AIStates.waiting_for_edit)
    await callback.message.answer("Пришлите исправленный текст ответа:")


@ai_router.message(AIStates.waiting_for_edit, SafeFreeText())
async def receive_ai_edit(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    short_id = data.get("chat_short_id")
    if not short_id:
        await state.clear()
        return
    await state.update_data(ai_draft=message.text)
    await state.set_state(None)
    await message.answer(message.text, reply_markup=keyboards.ai_draft_kb(short_id, allow_send=True))


@ai_router.callback_query(F.data.startswith(f"{constants.PREFIX_AICANCEL}_"))
async def cancel_ai_draft(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await callback.message.edit_text("❌ Черновик отменён.")
