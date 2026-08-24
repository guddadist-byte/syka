"""Async client for the official Avito Messenger API + a per-account pool.

KNOWN LIMITATION: exact endpoint paths/payload shapes below are best-effort
from public Avito Messenger API documentation for business accounts — no
live credentials were available while writing this, so they need a real
smoke test against a real client_id/client_secret before production use.
Everything endpoint-specific is isolated inside AvitoClient's methods so a
fix only ever touches this one file — tasks.py/handlers.py only ever see
the high-level interface (get_chats/get_messages/send_text_message/...).

The aiohttp.ClientSession passed into init_pool() must be a session with NO
proxy configured — Avito requests never go through the Telegram proxy
(config.py's proxy handling is entirely separate and only touches the
aiogram Bot's session).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import AsyncIterator

import aiohttp

import database
import models
import utils
from constants import AVITO_MAX_RETRIES, AVITO_MIN_REQUEST_INTERVAL_SECONDS, INFLIGHT_SHUTDOWN_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

AVITO_BASE_URL = "https://api.avito.ru"
TOKEN_URL = f"{AVITO_BASE_URL}/token"


class AvitoAPIError(Exception):
    pass


class AvitoAuthError(AvitoAPIError):
    pass


class AvitoRateLimitError(AvitoAPIError):
    pass


# --- graceful-shutdown tracking of in-flight sends --------------------------

_inflight_sends = 0
_inflight_zero_event = asyncio.Event()
_inflight_zero_event.set()


@asynccontextmanager
async def _track_inflight() -> AsyncIterator[None]:
    global _inflight_sends
    _inflight_sends += 1
    _inflight_zero_event.clear()
    try:
        yield
    finally:
        _inflight_sends -= 1
        if _inflight_sends <= 0:
            _inflight_sends = 0
            _inflight_zero_event.set()


async def wait_for_inflight_sends(timeout: float = INFLIGHT_SHUTDOWN_TIMEOUT_SECONDS) -> None:
    try:
        await asyncio.wait_for(_inflight_zero_event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("wait_for_inflight_sends: timed out with %d send(s) still in flight", _inflight_sends)


class AvitoClient:
    """One instance per avito_accounts row."""

    def __init__(self, account: models.AvitoAccount, session: aiohttp.ClientSession) -> None:
        self.account_id = account.id
        self.avito_user_id = account.avito_user_id
        self.client_id = account.client_id
        self.client_secret = account.client_secret
        self._session = session
        self._last_request_at = 0.0

    async def _ensure_token(self) -> str:
        account = await database.get_avito_account(self.account_id)
        if account and account.access_token and account.token_expires_at:
            expires = utils.parse_utc(account.token_expires_at)
            if expires - timedelta(seconds=60) > datetime.utcnow():
                return account.access_token
        return await self._refresh_token()

    async def _refresh_token(self) -> str:
        async with self._session.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise AvitoAuthError(f"token request failed: {resp.status}: {text}")
            data = await resp.json()
        token = data["access_token"]
        expires_in = int(data.get("expires_in", 3600))
        expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
        await database.update_avito_token(self.account_id, token, expires_at)
        return token

    async def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < AVITO_MIN_REQUEST_INTERVAL_SECONDS:
            await asyncio.sleep(AVITO_MIN_REQUEST_INTERVAL_SECONDS - elapsed)
        self._last_request_at = time.monotonic()

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        await self._throttle()
        token = await self._ensure_token()
        headers = kwargs.pop("headers", {}) or {}
        headers["Authorization"] = f"Bearer {token}"
        url = f"{AVITO_BASE_URL}{path}"

        backoff = 1.0
        allow_token_retry = True
        last_error: Exception | None = None

        for _attempt in range(AVITO_MAX_RETRIES):
            try:
                async with self._session.request(method, url, headers=headers, **kwargs) as resp:
                    if resp.status == 401 and allow_token_retry:
                        allow_token_retry = False
                        token = await self._refresh_token()
                        headers["Authorization"] = f"Bearer {token}"
                        continue
                    if resp.status == 429 or resp.status >= 500:
                        retry_after = resp.headers.get("Retry-After")
                        await asyncio.sleep(float(retry_after) if retry_after else backoff)
                        backoff *= 2
                        last_error = AvitoRateLimitError(f"{method} {path} -> {resp.status}")
                        continue
                    if resp.status >= 400:
                        text = await resp.text()
                        raise AvitoAPIError(f"{method} {path} -> {resp.status}: {text}")
                    if resp.content_length:
                        return await resp.json()
                    return {}
            except aiohttp.ClientError as exc:
                last_error = exc
                await asyncio.sleep(backoff)
                backoff *= 2

        raise AvitoAPIError(f"{method} {path} failed after retries: {last_error}")

    async def get_chats(self, unread_only: bool = False, limit: int = 100, offset: int = 0) -> list[models.AvitoChat]:
        params = {"limit": limit, "offset": offset}
        if unread_only:
            params["unread_only"] = "true"
        data = await self._request("GET", f"/messenger/v2/accounts/{self.avito_user_id}/chats", params=params)
        chats = []
        for raw in data.get("chats", []):
            last_message = raw.get("last_message") or {}
            chats.append(
                models.AvitoChat(
                    chat_id=raw["id"],
                    item_id=str(raw.get("context", {}).get("value", {}).get("id") or ""),
                    client_name=(raw.get("users") or [{}])[0].get("name", ""),
                    last_message_id=last_message.get("id"),
                    last_message_text=(last_message.get("content") or {}).get("text", ""),
                    last_message_direction="in" if last_message.get("direction") == "in" else "out",
                    last_message_at=str(last_message.get("created")),
                    unread_count=int(raw.get("unread") or 0),
                )
            )
        return chats

    async def get_messages(self, chat_id: str, limit: int = 50) -> list[models.AvitoMessage]:
        data = await self._request(
            "GET",
            f"/messenger/v3/accounts/{self.avito_user_id}/chats/{chat_id}/messages/",
            params={"limit": limit},
        )
        messages = []
        for raw in data.get("messages", []):
            content = raw.get("content") or {}
            messages.append(
                models.AvitoMessage(
                    message_id=raw["id"],
                    direction="in" if raw.get("direction") == "in" else "out",
                    text=content.get("text", ""),
                    has_image="image" in content,
                    created_at=str(raw.get("created")),
                )
            )
        return messages

    async def send_text_message(self, chat_id: str, text: str, message_uuid: str | None = None) -> models.AvitoMessage:
        message_uuid = message_uuid or str(uuid.uuid4())
        async with _track_inflight():
            data = await self._request(
                "POST",
                f"/messenger/v1/accounts/{self.avito_user_id}/chats/{chat_id}/messages",
                headers={"X-Idempotency-Key": message_uuid},
                json={"message": {"text": text}, "type": "text"},
            )
        return models.AvitoMessage(
            message_id=data.get("id", ""),
            direction="out",
            text=text,
            has_image=False,
            created_at=str(data.get("created", datetime.utcnow())),
        )

    async def upload_image(self, image_bytes: bytes, filename: str) -> str:
        form = aiohttp.FormData()
        form.add_field("uploadfile[]", image_bytes, filename=filename, content_type="image/jpeg")
        data = await self._request(
            "POST", f"/messenger/v1/accounts/{self.avito_user_id}/uploadImages", data=form
        )
        image_ids = list(data.keys()) if isinstance(data, dict) else []
        if not image_ids:
            raise AvitoAPIError("upload_image: no image id in response")
        return image_ids[0]

    async def send_image_message(self, chat_id: str, image_id: str, message_uuid: str | None = None) -> models.AvitoMessage:
        message_uuid = message_uuid or str(uuid.uuid4())
        async with _track_inflight():
            data = await self._request(
                "POST",
                f"/messenger/v1/accounts/{self.avito_user_id}/chats/{chat_id}/messages/image",
                headers={"X-Idempotency-Key": message_uuid},
                json={"image_id": image_id},
            )
        return models.AvitoMessage(
            message_id=data.get("id", ""),
            direction="out",
            text="",
            has_image=True,
            created_at=str(data.get("created", datetime.utcnow())),
        )

    async def mark_chat_read(self, chat_id: str) -> None:
        await self._request("POST", f"/messenger/v1/accounts/{self.avito_user_id}/chats/{chat_id}/read")

    async def get_item_info(self, item_id: str) -> models.AvitoItem:
        data = await self._request("GET", f"/core/v1/items/{item_id}")
        coords = data.get("coordinates") or {}
        return models.AvitoItem(
            item_id=item_id,
            lat=coords.get("lat"),
            lon=coords.get("lng") or coords.get("lon"),
            address_text=data.get("address"),
            title=data.get("title"),
        )

    async def delete_message(self, chat_id: str, message_id: str) -> None:
        await self._request(
            "DELETE", f"/messenger/v1/accounts/{self.avito_user_id}/chats/{chat_id}/messages/{message_id}"
        )


class AvitoClientPool:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._clients: dict[int, AvitoClient] = {}

    async def refresh_from_db(self) -> None:
        accounts = await database.list_avito_accounts(active_only=True)
        seen_ids: set[int] = set()
        for account in accounts:
            seen_ids.add(account.id)
            existing = self._clients.get(account.id)
            if (
                existing is None
                or existing.client_id != account.client_id
                or existing.client_secret != account.client_secret
            ):
                self._clients[account.id] = AvitoClient(account, self._session)
        for stale_id in set(self._clients) - seen_ids:
            del self._clients[stale_id]

    def get(self, account_id: int) -> AvitoClient | None:
        return self._clients.get(account_id)

    def all(self) -> list[AvitoClient]:
        return list(self._clients.values())


_pool: AvitoClientPool | None = None


async def init_pool(session: aiohttp.ClientSession) -> None:
    global _pool
    _pool = AvitoClientPool(session)
    await _pool.refresh_from_db()


def get_pool() -> AvitoClientPool:
    if _pool is None:
        raise RuntimeError("avito_client.init_pool() has not been called yet")
    return _pool


async def reload_accounts() -> None:
    await get_pool().refresh_from_db()
