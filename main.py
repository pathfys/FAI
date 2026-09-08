import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

import config
from database import init_db
from handlers import accounts_router, campaigns_router, start_router
from handlers.middleware import AdminOnlyMiddleware

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(config.LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


async def main() -> None:
    logger.info("Initializing database...")
    await init_db()

    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
    )
    dp = Dispatcher(storage=MemoryStorage())

    dp.message.middleware(AdminOnlyMiddleware())
    dp.callback_query.middleware(AdminOnlyMiddleware())

    dp.include_router(start_router)
    dp.include_router(accounts_router)
    dp.include_router(campaigns_router)

    logger.info("Bot starting...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
