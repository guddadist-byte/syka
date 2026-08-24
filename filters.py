"""RoleAtLeast, ApprovedUser, SafeFreeText — aiogram filters."""

from __future__ import annotations

from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message

import constants
import database


class ApprovedUser(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user = await database.get_user(event.from_user.id)
        return bool(user) and user.status == constants.STATUS_APPROVED


class RoleAtLeast(BaseFilter):
    def __init__(self, min_role: str) -> None:
        self.min_role = min_role

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user = await database.get_user(event.from_user.id)
        if not user or user.status != constants.STATUS_APPROVED:
            return False
        return constants.ROLE_ORDER.get(user.role, -1) >= constants.ROLE_ORDER[self.min_role]


class SafeFreeText(BaseFilter):
    """Rejects slash-commands and any known reply-keyboard button label.

    Attach to every free-text-capturing handler (a reply to a client, an AI
    edit, an admin form field) as defense-in-depth alongside the router
    registration order in handlers.py — see the State Guard notes there.
    """

    async def __call__(self, message: Message) -> bool:
        text = message.text or ""
        if text.startswith("/"):
            return False
        return text not in constants.ALL_KNOWN_BUTTON_TEXTS
