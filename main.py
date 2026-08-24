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

import ai_handlers
import avito_client
import bot_cache
import config
import database
import handlers
import tasks
import utils

logger = logging.getLogger(__name__)


async def main() -> None:
    static_cfg = config.load_static_config()
    logging.basicConfig(level=static_cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    lock_handle = utils.acquire_singleton_lock(static_cfg.pid_file)
    try:
        await database.init_db(static_cfg.db_path)
        await database.bootstrap_director(static_cfg.superadmin_telegram_id)
        await database.seed_from_credentials_file(static_cfg.credentials_path)
        await bot_cache.init_cache()

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

        avito_session = aiohttp.ClientSession()
        try:
            await avito_client.init_pool(avito_session)
            await avito_client.reload_accounts()

            poll_tasks = await tasks.run_all_polls(bot, static_cfg.db_path)
            try:
                await dp.start_polling(bot)
            finally:
                await tasks.stop_all(poll_tasks)
                await avito_client.wait_for_inflight_sends()
        finally:
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
