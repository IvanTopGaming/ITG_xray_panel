import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

import config
from backend_client import BackendClient
from bot_events_consumer import run_consumer
from handlers import catalog, user
from i18n import I18n
from middleware import LangMiddleware
from runtime_config import runtime_config
import screens


logger = logging.getLogger(__name__)


def _build_bot() -> Bot:
    session = None
    if runtime_config.telegram_proxy_url:
        logger.info("bot: routing Telegram via configured HTTP proxy")
        session = AiohttpSession(proxy=runtime_config.telegram_proxy_url)
    return Bot(
        token=runtime_config.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


async def _report_identity(bot) -> None:
    try:
        me = await bot.get_me()
    except Exception as exc:
        logger.warning("could not ask Telegram for this bot's username: %s", type(exc).__name__)
        return
    runtime_config.set_bot_username(me.username or "")


async def main() -> None:
    logging.basicConfig(
        level=getattr(logging, config.BOT_LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if not config.BOT_SERVICE_TOKEN:
        logger.warning(
            "BOT_SERVICE_TOKEN is not set — idling. Generate one via the panel UI "
            "(Bot → Settings → Rotate token), put it in the bot's .env, then "
            "restart the bot service. Sleeping until then."
        )
        while True:
            await asyncio.sleep(3600)

    await runtime_config.bootstrap()

    bot = _build_bot()
    dp = Dispatcher(storage=MemoryStorage())

    backend = BackendClient()
    i18n = I18n(backend)

    middleware = LangMiddleware(backend, i18n)
    dp.message.middleware(middleware)
    dp.callback_query.middleware(middleware)

    dp.include_router(user.router)
    dp.include_router(catalog.router)
    dp.include_router(screens.router)

    state = {"bot": bot}
    restart = asyncio.Event()

    async def on_runtime_change(session_changed: bool) -> None:
        if session_changed:
            restart.set()

    runtime_config.set_change_listener(on_runtime_change)
    refresh_task = asyncio.create_task(runtime_config.refresh_loop())
    consumer_task = asyncio.create_task(run_consumer(lambda: state["bot"], i18n, middleware, backend=backend))

    polling_task = None
    restart_task = None
    try:
        while True:
            await _report_identity(state["bot"])
            await state["bot"].delete_webhook(drop_pending_updates=False)
            logger.info("bot started, polling Telegram")
            polling_task = asyncio.create_task(dp.start_polling(state["bot"], close_bot_session=False))
            restart_task = asyncio.create_task(restart.wait())
            done, _ = await asyncio.wait((polling_task, restart_task), return_when=asyncio.FIRST_COMPLETED)
            if polling_task in done:
                await polling_task
                break
            restart.clear()
            await dp.stop_polling()
            await polling_task
            polling_task = None
            restart_task = None
            logger.info("runtime-change: rebuilding aiogram session")
            new_bot = _build_bot()
            old_bot = state["bot"]
            state["bot"] = new_bot
            await old_bot.session.close()
    finally:
        if restart_task is not None:
            restart_task.cancel()
            await asyncio.gather(restart_task, return_exceptions=True)
        if polling_task is not None and not polling_task.done():
            try:
                await dp.stop_polling()
            except RuntimeError:
                polling_task.cancel()
            await asyncio.gather(polling_task, return_exceptions=True)
        refresh_task.cancel()
        consumer_task.cancel()
        results = await asyncio.gather(refresh_task, consumer_task, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.error("bot background task stopped with %s", type(result).__name__)
        await catalog.close_checkout_tasks()
        await backend.close()
        await runtime_config.close()
        await state["bot"].session.close()


if __name__ == "__main__":
    asyncio.run(main())
