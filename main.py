"""Entrypoint: startup/shutdown sequencing.

Order matters here and is deliberate (see the plan for the full
rationale): the singleton file lock is the very first thing acquired,
before any DB/network access, so a systemd instance and an accidental
manual `python main.py` (e.g. from ssh) against the same DB can never
both run pollers at once. Graceful shutdown, symmetrically, is the last
thing to happen — it waits for any in-flight Avito sends before closing
sessions, so a deploy/restart never silently drops a reply that was
already on the wire.
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp
from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import MenuButtonWebApp, WebAppInfo
from aiohttp import web

import ai_handlers
import avito_client
import bot_cache
import config
import constants
import database
import handlers
import keyboards
import tasks
import utils
import webapp

logger = logging.getLogger(__name__)


async def main() -> None:
    static_cfg = config.load_static_config()
    logging.basicConfig(level=static_cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    keyboards.set_webapp_url(static_cfg.webapp_url)

    lock_handle = utils.acquire_singleton_lock(static_cfg.pid_file)
    try:
        await database.init_db(static_cfg.db_path)
        await database.bootstrap_director(static_cfg.superadmin_telegram_id)
        await database.seed_from_credentials_file(static_cfg.credentials_path)
        await database.backfill_fallback_items()
        await bot_cache.init_cache()
        await tasks.hydrate_cache_from_db()

        bot = await config.build_bot(static_cfg)

        dp = Dispatcher(storage=MemoryStorage())
        # Injected into every handler's `data` so a handler can accept it
        # as a plain parameter (e.g. `fsm_storage: BaseStorage`) — used by
        # the instant-block flow to force-clear a blocked user's FSM state.
        dp["fsm_storage"] = dp.storage

        dp.include_router(handlers.commands_router)
        dp.include_router(handlers.menu_router)
        dp.include_router(handlers.registration_router)
        dp.include_router(handlers.crm_router)
        dp.include_router(ai_handlers.ai_router)
        dp.include_router(handlers.template_router)
        dp.include_router(handlers.admin_router)
        dp.include_router(handlers.settings_router)
        # Last on purpose: it answers only what no other router claimed, so
        # a message sent into a form that is no longer open gets a reply
        # instead of vanishing. Anything registered after it would be dead.
        dp.include_router(handlers.fallback_router)

        # An explicit timeout, because aiohttp's default is total=5min per
        # attempt and _request retries up to AVITO_MAX_RETRIES times — one
        # unlucky request could hold an account's strictly serial poll loop
        # for a quarter of an hour, with every other chat on that account
        # silent behind it.
        avito_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=constants.AVITO_REQUEST_TIMEOUT_SECONDS)
        )
        web_runner: web.AppRunner | None = None
        try:
            await avito_client.init_pool(avito_session)
            await avito_client.reload_accounts()

            if static_cfg.webapp_url:
                try:
                    await bot.set_chat_menu_button(
                        menu_button=MenuButtonWebApp(text="📱 Приложение", web_app=WebAppInfo(url=static_cfg.webapp_url))
                    )
                except Exception:
                    # This is the first live Telegram call of the whole
                    # startup, and it used to be unguarded — so whenever
                    # Telegram was unreachable the bot died right here,
                    # before run_all_polls and before start_polling, and
                    # systemd restarted it forever. From the outside that
                    # looks exactly like "the service is running but the
                    # bot is silent". The Mini App also opens from the
                    # bot's own main menu, so losing the system menu
                    # button costs far less than not starting at all.
                    logger.warning("could not set the Mini App menu button, continuing without it", exc_info=True)
                web_app = webapp.create_app(static_cfg.bot_token, bot, static_cfg.db_path)
                web_runner = web.AppRunner(web_app)
                await web_runner.setup()
                site = web.TCPSite(web_runner, static_cfg.webapp_host, static_cfg.webapp_port)
                await site.start()
                logger.info("Mini App backend listening on %s:%s", static_cfg.webapp_host, static_cfg.webapp_port)

            poll_tasks = await tasks.run_all_polls(bot, static_cfg.db_path)
            # Sent from here, not earlier: by this point the DB, the cache,
            # the Avito pool, the web server and every poller are up, so the
            # report describes a bot that is genuinely working.
            await tasks.send_startup_report(bot, webapp_enabled=bool(static_cfg.webapp_url))
            try:
                # The line that separates "came up" from "crash-looping":
                # on a healthy start the log was otherwise almost empty,
                # so there was nothing to look for.
                logger.info("polling started — the bot is live")
                await dp.start_polling(bot)
            finally:
                # Marks this as a planned stop. A start that finds no such
                # mark newer than the last heartbeat knows the process died
                # instead of being deployed over.
                await database.mark_graceful_stop()
                await tasks.stop_all(poll_tasks)
                await avito_client.wait_for_inflight_sends()
        finally:
            if web_runner is not None:
                await web_runner.cleanup()
            await avito_session.close()
            await bot.session.close()
    finally:
        await database.close_db()
        lock_handle.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except utils.SingletonLockError as exc:
        logging.basicConfig(level="INFO")
        logger.error(str(exc))
        raise SystemExit(1)
    except Exception:
        # main() has no except of its own, only finally blocks, so without
        # this any startup failure left a bare traceback on stderr and the
        # process exited — with Restart=always that is an endless restart
        # loop wearing the disguise of a running service. Name it instead.
        logging.basicConfig(level="INFO")
        logger.exception("bot failed to start")
        raise SystemExit(1)
