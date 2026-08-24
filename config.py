"""Static .env bootstrap + DB-backed runtime settings + Bot construction."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from dotenv import load_dotenv

import database


@dataclass
class StaticConfig:
    bot_token: str
    db_path: str
    superadmin_telegram_id: int
    log_level: str
    pid_file: str
    credentials_path: str


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set in .env")
    return value


def load_static_config() -> StaticConfig:
    load_dotenv()

    db_path = _require_env("DB_PATH")
    pid_file = os.environ.get("PID_FILE") or str(Path(db_path).with_suffix(".pid"))
    credentials_path = os.environ.get("CREDENTIALS_PATH") or str(Path(db_path).parent / "credentials.toml")

    return StaticConfig(
        bot_token=_require_env("BOT_TOKEN"),
        db_path=db_path,
        superadmin_telegram_id=int(_require_env("SUPERADMIN_TELEGRAM_ID")),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        pid_file=pid_file,
        credentials_path=credentials_path,
    )


def _proxy_url_with_auth(proxy_url: str, login: str | None, password: str | None) -> str:
    if not login or "@" in proxy_url:
        return proxy_url
    scheme, _, rest = proxy_url.partition("://")
    auth = f"{login}:{password or ''}"
    return f"{scheme}://{auth}@{rest}"


async def build_bot(static_cfg: StaticConfig) -> Bot:
    """Construct the aiogram Bot, optionally routed through the DB-configured
    proxy. This proxy is ONLY for the Telegram Bot API connection — Avito
    requests (avito_client.py) use a completely separate aiohttp session
    with no proxy at all.
    """
    proxy_cfg = await database.get_proxy_config()

    session: AiohttpSession | None = None
    if proxy_cfg.is_enabled and proxy_cfg.proxy_url:
        proxy_url = _proxy_url_with_auth(proxy_cfg.proxy_url, proxy_cfg.proxy_login, proxy_cfg.proxy_password)
        # aiogram's AiohttpSession accepts both http(s):// and socks4/5://
        # proxy URLs uniformly (socks support requires aiohttp-socks, which
        # is in requirements.txt).
        session = AiohttpSession(proxy=proxy_url)

    return Bot(
        token=static_cfg.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
