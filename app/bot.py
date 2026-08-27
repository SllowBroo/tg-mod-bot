from __future__ import annotations

import asyncio
import logging
import re
import time

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from app.config import Config, load_config
from app.database import Database
from app.handlers import ads, errors, moderation, replies, welcome
from app.middlewares import CommandRateLimitMiddleware, RememberUsersMiddleware
from app.scheduler import AdScheduler

TOKEN_PATTERN = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b")


class SecretRedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return TOKEN_PATTERN.sub("[REDACTED]", super().format(record))


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        SecretRedactingFormatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)


def build_dispatcher(
    config: Config, db: Database, scheduler: AdScheduler
) -> Dispatcher:
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher["db"] = db
    dispatcher["ad_scheduler"] = scheduler
    dispatcher.message.outer_middleware(RememberUsersMiddleware())
    dispatcher.message.middleware(CommandRateLimitMiddleware(config.command_cooldown))

    dispatcher.include_router(ads.router)
    dispatcher.include_router(welcome.router)
    dispatcher.include_router(replies.router)
    dispatcher.include_router(moderation.router)
    dispatcher.include_router(errors.router)
    dispatcher.errors.register(errors.global_error_handler)
    return dispatcher


async def run_bot(config: Config | None = None) -> None:
    config = config or load_config()
    configure_logging(config.log_level)
    logger = logging.getLogger(__name__)

    db = Database(config.database_path)
    await db.initialize()
    if config.allowed_chat_id is not None:
        await db.set_allowed_chat_id(config.allowed_chat_id)

    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    scheduler = AdScheduler(bot, db)
    dispatcher = build_dispatcher(config, db, scheduler)

    try:
        me = await bot.get_me()
        logger.info("Бот @%s запущен; база: %s", me.username, config.database_path)
        await bot.delete_webhook(drop_pending_updates=False)
        await scheduler.start()
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
            close_bot_session=False,
        )
    finally:
        await scheduler.shutdown()
        await bot.session.close()


def run_forever() -> None:
    """Run polling and restart it after unexpected failures."""
    config = load_config()
    configure_logging(config.log_level)
    logger = logging.getLogger(__name__)
    delay = 3
    while True:
        try:
            asyncio.run(run_bot(config))
            return
        except (KeyboardInterrupt, SystemExit):
            logger.info("Остановка по сигналу")
            return
        except Exception as exc:  # noqa: BLE001 - top-level crash guard
            logger.error(
                "Процесс polling завершился с ошибкой %s; повтор через %s с",
                type(exc).__name__,
                delay,
            )
            try:
                time.sleep(delay)
            except KeyboardInterrupt:
                return
            delay = min(delay * 2, 60)
