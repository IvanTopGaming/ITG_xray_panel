import asyncio
import logging
import os
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from config import BOT_TOKEN
from api_service import panel_api
from handlers import user, admin
from jobs import send_backup, check_and_notify_users, sync_users_across_panels


async def main():
    log_level = str(os.getenv("BOT_LOG_LEVEL", "INFO")).upper()
    logging.basicConfig(level=getattr(logging, log_level, logging.INFO))

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    dp.include_router(admin.router)
    dp.include_router(user.router)

    scheduler = AsyncIOScheduler()

    scheduler.add_job(send_backup, "cron", hour=0, minute=0, args=[bot])

    scheduler.add_job(check_and_notify_users, "cron", hour=12, minute=0, args=[bot])

    scheduler.add_job(sync_users_across_panels, "interval", minutes=60, args=[bot])

    scheduler.start()

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        await panel_api.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
