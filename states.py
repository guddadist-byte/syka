"""aiogram FSM StatesGroup definitions only — no other imports."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class ReplyStates(StatesGroup):
    waiting_for_text = State()
    waiting_for_photo = State()


class AIStates(StatesGroup):
    waiting_for_edit = State()
    waiting_for_custom_prompt = State()


class RegistrationStates(StatesGroup):
    waiting_for_payment = State()
    waiting_for_full_name = State()
    waiting_for_trade_point = State()


class AdminStates(StatesGroup):
    # Points
    waiting_for_point_name = State()
    waiting_for_point_address = State()
    waiting_for_point_hours = State()
    waiting_for_point_code = State()
    waiting_for_point_coordinate = State()
    waiting_for_responsible_point = State()
    waiting_for_bulk_points_import = State()

    # Avito accounts
    waiting_for_avito_name = State()
    waiting_for_avito_client_id = State()
    waiting_for_avito_client_secret = State()
    waiting_for_avito_point = State()

    # AI settings
    waiting_for_ai_base_url = State()
    waiting_for_ai_model = State()
    waiting_for_ai_api_key = State()
    waiting_for_ai_header_name = State()
    waiting_for_ai_header_value = State()

    # Proxy settings
    waiting_for_proxy_url = State()
    waiting_for_proxy_login = State()
    waiting_for_proxy_password = State()

    # Payment settings
    waiting_for_payment_amount = State()

    # Welcome message
    waiting_for_welcome_text = State()

    # Backup settings
    waiting_for_backup_interval = State()
    waiting_for_backup_recipient = State()

    # Broadcast composer
    waiting_for_broadcast_text = State()
    waiting_for_broadcast_photo = State()

    # Templates
    waiting_for_template_title = State()
    waiting_for_template_body = State()

    # User editing (Все пользователи)
    waiting_for_user_fullname = State()
    waiting_for_user_trade_point = State()
    waiting_for_unblock_telegram_id = State()
    waiting_for_delete_telegram_id = State()

    # Reviews (Отзывы Avito)
    waiting_for_review_answer = State()

    # Orders (Заказы Avito Доставки)
    waiting_for_order_markings = State()
    waiting_for_cnc_address = State()
    waiting_for_cnc_period = State()
    waiting_for_cnc_comment = State()
