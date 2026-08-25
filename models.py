"""Plain dataclasses mirroring database rows / domain objects.

Leaf module (only stdlib deps) so every other module can import it freely.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


def _row(cls, row: Mapping[str, Any]):
    fields = {f for f in cls.__dataclass_fields__}
    return cls(**{k: v for k, v in dict(row).items() if k in fields})


@dataclass
class User:
    telegram_id: int
    username: str | None
    full_name: str | None
    last_name: str | None
    role: str
    status: str
    on_shift: int
    responsible_point_id: int | None
    blocked_bot: int
    rating_points: int
    created_at: str
    approved_at: str | None
    approved_by: int | None
    last_seen_at: str | None
    last_start_at: str | None
    trade_point_name: str | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "User":
        return _row(cls, row)


@dataclass
class Point:
    id: int
    name: str
    address: str | None
    working_hours: str | None
    name_is_custom: int
    is_active: int
    created_at: str
    code: str | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Point":
        return _row(cls, row)


@dataclass
class PointCoordinate:
    id: int
    point_id: int
    lat: float
    lon: float
    source: str
    created_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "PointCoordinate":
        return _row(cls, row)


@dataclass
class AvitoAccount:
    id: int
    point_id: int | None
    avito_user_id: int
    name: str
    client_id: str
    client_secret: str
    access_token: str | None
    token_expires_at: str | None
    is_active: int
    last_poll_at: str | None
    last_poll_error: str | None
    created_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "AvitoAccount":
        return _row(cls, row)


@dataclass
class Template:
    id: int
    point_id: int | None
    kind: str
    title: str
    body: str
    created_by: int | None
    created_at: str
    is_active: int

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Template":
        return _row(cls, row)


@dataclass
class AIConfig:
    id: int
    base_url: str
    model: str
    api_key: str | None
    extra_header_name: str
    extra_header_value: str
    system_prompt: str | None
    is_enabled: int
    updated_at: str
    updated_by: int | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "AIConfig":
        return _row(cls, row)


@dataclass
class ProxyConfig:
    id: int
    is_enabled: int
    proxy_url: str | None
    proxy_login: str | None
    proxy_password: str | None
    updated_at: str
    updated_by: int | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "ProxyConfig":
        return _row(cls, row)


@dataclass
class PaymentConfig:
    id: int
    is_enabled: int
    amount_stars: int
    updated_at: str
    updated_by: int | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "PaymentConfig":
        return _row(cls, row)


@dataclass
class Payment:
    id: int
    user_id: int
    telegram_charge_id: str
    amount_stars: int
    paid_at: str
    refunded_at: str | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Payment":
        return _row(cls, row)


@dataclass
class BackupConfig:
    id: int
    is_enabled: int
    interval_hours: int
    recipient_telegram_id: int | None
    last_backup_at: str | None
    updated_at: str
    updated_by: int | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "BackupConfig":
        return _row(cls, row)


@dataclass
class ChatSummary:
    chat_id: str
    avito_account_id: int
    point_id: int | None
    item_id: str | None
    client_name: str | None
    last_message_at: str | None
    last_message_text: str | None
    last_message_dir: str | None
    unread_count: int
    last_replied_by: int | None
    last_replied_at: str | None
    updated_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "ChatSummary":
        return _row(cls, row)


@dataclass
class Message:
    id: int
    chat_id: str
    avito_message_id: str | None
    message_uuid: str | None
    direction: str
    text: str | None
    has_image: int
    sent_at: str
    created_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Message":
        return _row(cls, row)


# --- Avito API domain objects (not DB rows) ---------------------------------


@dataclass
class AvitoChat:
    chat_id: str
    item_id: str | None
    client_name: str
    last_message_id: str | None
    last_message_text: str
    last_message_direction: str
    last_message_at: str
    unread_count: int
    item_lat: float | None = None
    item_lon: float | None = None
    location_title: str | None = None
    item_title: str | None = None
    item_url: str | None = None


@dataclass
class AvitoMessage:
    message_id: str
    direction: str
    text: str
    has_image: bool
    created_at: str

