"""Годится ли этот прокси для бота — ответ до того, как вписывать его в БД.

Запуск на сервере:
    cd /opt/avito_bot && venv/bin/python3 tools/check_proxy.py socks5://user:pass@host:port

С флагом --apply прокси вдобавок вписывается в конфиг бота — но только
если проверка прошла. Негодный прокси в конфиг не попадёт:
    cd /opt/avito_bot && venv/bin/python3 tools/check_proxy.py --apply socks5://...

Токен берётся из .env рядом с проектом (или из BOT_TOKEN в окружении), так
что вводить его отдельно не нужно. Проверка идёт по шагам, от «жив ли
прокси вообще» к «доходит ли он до Telegram» — потому что эти два отказа
лечатся совершенно по-разному, а в логе бота выглядят одинаково.

Без --apply ничего не меняет: только исходящие запросы.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

NEUTRAL_URL = "https://api.ipify.org"
TELEGRAM_HOST = "https://api.telegram.org"
TIMEOUT = 20


def parse_proxy(url: str) -> tuple[str, str, int]:
    """-> (схема, хост, порт). Бросает ValueError с понятным текстом."""
    parsed = urlparse(url)
    if parsed.scheme not in ("socks5", "socks5h", "socks4", "http", "https"):
        raise ValueError(
            f"непонятная схема {parsed.scheme!r}; нужен socks5:// или http://"
        )
    if not parsed.hostname:
        raise ValueError("в URL нет хоста")
    port = parsed.port
    if port is None:
        raise ValueError("в URL не указан порт")
    return parsed.scheme, parsed.hostname, port


def read_env_value(name: str) -> str | None:
    """Значение из окружения, иначе из .env рядом с проектом."""
    value = os.environ.get(name)
    if value:
        return value
    env = Path(__file__).resolve().parent.parent / ".env"
    if not env.exists():
        return None
    prefix = f"{name}="
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith(prefix):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def read_bot_token() -> str | None:
    return read_env_value("BOT_TOKEN")


def apply_proxy(url: str) -> str:
    """Записывает прокси в proxy_config. Вызывается ТОЛЬКО при вердикте
    «годится» — негодный прокси не должен иметь физической возможности
    попасть в конфиг, это и есть смысл флага."""
    import sqlite3

    db_path = read_env_value("DB_PATH")
    if not db_path:
        raise RuntimeError("не нашёл DB_PATH ни в окружении, ни в .env")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE proxy_config SET proxy_url = ?, proxy_login = NULL, "
            "proxy_password = NULL, is_enabled = 1 WHERE id = 1",
            (url,),
        )
        conn.commit()
        row = conn.execute(
            "SELECT is_enabled, proxy_url FROM proxy_config WHERE id = 1"
        ).fetchone()
    finally:
        conn.close()
    return f"{row[0]}, {row[1]}"


def make_connector(url: str):
    scheme, _, _ = parse_proxy(url)
    if scheme.startswith("socks"):
        try:
            from aiohttp_socks import ProxyConnector
        except ImportError:
            raise RuntimeError(
                "в этом окружении нет aiohttp-socks — поставьте "
                "venv/bin/pip install -r requirements.txt"
            )
        return ProxyConnector.from_url(url), None
    return aiohttp.TCPConnector(), url


async def step_tcp(host: str, port: int) -> str | None:
    """None — порт открылся; иначе текст причины."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=TIMEOUT
        )
        writer.close()
        return None
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


async def step_fetch(url: str, target: str) -> tuple[bool, str]:
    connector, http_proxy = make_connector(url)
    timeout = aiohttp.ClientTimeout(total=TIMEOUT)
    try:
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as s:
            async with s.get(target, proxy=http_proxy) as resp:
                body = (await resp.text())[:300]
                return resp.status < 400, f"HTTP {resp.status}: {body.strip()}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def verdict(tcp_err, neutral_ok, neutral_msg, tg_ok, tg_msg) -> tuple[bool, str]:
    """Три разных отказа, три разных вывода — в этом весь смысл скрипта."""
    if tcp_err is not None:
        return False, (
            "НЕ ГОДИТСЯ: сам прокси недоступен — до его адреса и порта нет "
            f"соединения ({tcp_err}). Прокси выключен, просрочен или закрыт "
            "файрволом."
        )
    if not neutral_ok:
        return False, (
            "НЕ ГОДИТСЯ: прокси отвечает, но наружу через него ничего не "
            f"проходит ({neutral_msg}). Скорее всего неверные логин/пароль "
            "или у прокси нет исходящего доступа."
        )
    if tg_ok:
        return True, "ГОДИТСЯ: через этот прокси Telegram отвечает."
    if "401" in tg_msg or "Unauthorized" in tg_msg:
        return False, (
            "ПРОКСИ ГОДИТСЯ, НО ТОКЕН ОТОЗВАН: Telegram доступен, но этот бот-токен "
            "он не принимает. Нужен новый токен у @BotFather."
        )
    return False, (
        "НЕ ГОДИТСЯ: прокси живой и в интернет ходит, но до Telegram не "
        f"доходит ({tg_msg}). Нужен прокси из сети, откуда Telegram открыт."
    )


async def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--apply"]
    do_apply = "--apply" in sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    url = args[0]

    try:
        scheme, host, port = parse_proxy(url)
    except ValueError as exc:
        print(f"Неверный URL прокси: {exc}")
        return 2

    print(f"Прокси: {scheme}://{host}:{port}\n")

    print("1. Отвечает ли сам прокси...")
    tcp_err = await step_tcp(host, port)
    print("   " + ("порт открыт" if tcp_err is None else f"НЕТ — {tcp_err}"))

    neutral_ok, neutral_msg = False, "не проверялось"
    tg_ok, tg_msg = False, "не проверялось"

    if tcp_err is None:
        print("2. Ходит ли через него интернет...")
        try:
            neutral_ok, neutral_msg = await step_fetch(url, NEUTRAL_URL)
        except RuntimeError as exc:
            print(f"   {exc}")
            return 2
        print("   " + (f"да, внешний адрес {neutral_msg}" if neutral_ok else f"НЕТ — {neutral_msg}"))

        if neutral_ok:
            token = read_bot_token()
            if not token:
                print("3. Telegram: пропущено — не нашёл BOT_TOKEN ни в окружении, ни в .env")
                print("\nПРОВЕРКА НЕПОЛНАЯ: прокси живой, но до Telegram не дотянулись.")
                return 2
            print("3. Доходит ли до Telegram...")
            tg_ok, tg_msg = await step_fetch(url, f"{TELEGRAM_HOST}/bot{token}/getMe")
            print("   " + ("да" if tg_ok else f"НЕТ — {tg_msg}"))

    ok, text = verdict(tcp_err, neutral_ok, neutral_msg, tg_ok, tg_msg)
    print(f"\n{text}")

    if do_apply:
        if not ok:
            print("\nВ конфиг НЕ вписан — негодный прокси бот не получит.")
        else:
            try:
                state = apply_proxy(url)
            except Exception as exc:
                print(f"\nПроверка прошла, но записать в конфиг не вышло: {exc}")
                return 1
            print(f"\nВписан в конфиг (is_enabled, proxy_url) = ({state})")
            print("Осталось перезапустить:  sudo systemctl restart avito_bot")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
