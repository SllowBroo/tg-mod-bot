from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.database import Database

logger = logging.getLogger(__name__)


class AdScheduler:
    def __init__(self, bot: Bot, db: Database) -> None:
        self.bot = bot
        self.db = db
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        self._started = False

    @staticmethod
    def _job_id(chat_id: int) -> str:
        return f"advertisement:{chat_id}"

    async def start(self) -> None:
        if self._started:
            return
        self._scheduler.start()
        self._started = True
        for chat_id in await self.db.get_enabled_ad_chats():
            settings = await self.db.get_settings(chat_id)
            self.schedule(chat_id, settings.ad_interval_seconds)
        logger.info("Планировщик рекламы запущен")

    def schedule(self, chat_id: int, interval_seconds: int) -> None:
        if not self._started:
            raise RuntimeError("Планировщик ещё не запущен")
        self._scheduler.add_job(
            self.send_ad,
            trigger=IntervalTrigger(seconds=interval_seconds),
            kwargs={"chat_id": chat_id},
            id=self._job_id(chat_id),
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=60,
        )

    def unschedule(self, chat_id: int) -> None:
        job = self._scheduler.get_job(self._job_id(chat_id))
        if job:
            self._scheduler.remove_job(job.id)

    async def send_ad(self, chat_id: int, *, raise_errors: bool = False) -> bool:
        settings = await self.db.get_settings(chat_id)
        try:
            if settings.ad_photo_file_id:
                caption = settings.ad_caption or settings.ad_text
                await self.bot.send_photo(
                    chat_id,
                    settings.ad_photo_file_id,
                    caption=caption,
                    parse_mode="HTML",
                )
            elif settings.ad_text:
                await self.bot.send_message(
                    chat_id, settings.ad_text, parse_mode="HTML"
                )
            else:
                return False
        except TelegramAPIError:
            logger.exception(
                "Telegram API отклонил отправку рекламы в chat_id=%s", chat_id
            )
            if raise_errors:
                raise
            return False
        return True

    async def shutdown(self) -> None:
        if not self._started:
            return
        self._scheduler.shutdown(wait=False)
        self._started = False
        logger.info("Планировщик рекламы остановлен")
