import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

import config
from api_service import panel_api
from backend_client import BackendClient
from bot_events_consumer import run_consumer
from handlers import admin, catalog, user
from i18n import I18n
from middleware import LangMiddleware
from runtime_config import runtime_config


logger = logging.getLogger(__name__)


def _build_bot() -> Bot:
    session = None
    if runtime_config.telegram_proxy_url:
        logger.info("bot: routing Telegram via HTTP proxy %s", runtime_config.telegram_proxy_url)
        session = AiohttpSession(proxy=runtime_config.telegram_proxy_url)
    return Bot(
        token=runtime_config.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


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
    await panel_api.reload_from_runtime()

    bot = _build_bot()
    dp = Dispatcher(storage=MemoryStorage())

    backend = BackendClient()
    i18n = I18n(backend)

    middleware = LangMiddleware(backend, i18n)
    dp.message.middleware(middleware)
    dp.callback_query.middleware(middleware)

    dp.include_router(admin.router)
    dp.include_router(user.router)
    dp.include_router(catalog.router)

    state: dict[str, object] = {"bot": bot}

    async def on_runtime_change(session_changed: bool) -> None:
        try:
            await panel_api.reload_from_runtime()
        except Exception as exc:
            logger.exception("runtime-change: panel_api reload failed: %s", exc)
        if not session_changed:
            return

        logger.info("runtime-change: rebuilding aiogram session")
        old_bot = state["bot"]
        try:
            await dp.stop_polling()
        except Exception as exc:
            logger.warning("runtime-change: stop_polling: %s", exc)
        try:
            await old_bot.session.close()  # type: ignore[union-attr]
        except Exception as exc:
            logger.warning("runtime-change: close old session: %s", exc)

        new_bot = _build_bot()
        state["bot"] = new_bot
        try:
            await new_bot.delete_webhook(drop_pending_updates=True)
        except Exception as exc:
            logger.warning("runtime-change: delete_webhook: %s", exc)
        asyncio.create_task(dp.start_polling(new_bot))

    runtime_config.set_change_listener(on_runtime_change)
    refresh_task = asyncio.create_task(runtime_config.refresh_loop())
    consumer_task = asyncio.create_task(run_consumer(lambda: state["bot"], i18n, middleware, backend=backend))

    logger.info("bot started, polling Telegram")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        refresh_task.cancel()
        consumer_task.cancel()
        for t in (refresh_task, consumer_task):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        await backend.close()
        await panel_api.close()
        await runtime_config.close()
        try:
            await state["bot"].session.close()  # type: ignore[union-attr]
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
