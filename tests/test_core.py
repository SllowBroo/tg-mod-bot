from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from aiogram import Bot
from aiogram.types import User

from app.database import Database
from app.scheduler import AdScheduler
from app.utils import parse_duration, render_welcome

DUMMY_TOKEN = f"{123456}:{'A' * 32}"


class DatabaseAndSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "bot.db")
        await self.db.initialize()

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_settings_users_replies_and_warnings_persist(self) -> None:
        chat_id = -1001234567890
        await self.db.set_allowed_chat_id(chat_id)
        await self.db.update_settings(
            chat_id,
            ad_text="<b>Реклама</b>",
            ad_interval_seconds=300,
            ad_enabled=True,
            welcome_text="Привет, {mention}!",
            welcome_enabled=True,
        )
        await self.db.set_quick_reply(chat_id, "buy_dzo", "Ответ")
        await self.db.remember_user(chat_id, 42, "TestUser", "Test User")

        self.assertEqual(await self.db.get_allowed_chat_id(), chat_id)
        settings = await self.db.get_settings(chat_id)
        self.assertTrue(settings.ad_enabled)
        self.assertEqual(settings.ad_interval_seconds, 300)
        self.assertEqual(await self.db.get_quick_reply(chat_id, "buy_dzo"), "Ответ")
        self.assertEqual(await self.db.find_user_by_username(chat_id, "@testuser"), 42)
        self.assertEqual(await self.db.change_warning(chat_id, 42, 1), 1)
        self.assertEqual(await self.db.change_warning(chat_id, 42, -1), 0)

    async def test_scheduler_starts_once_and_replaces_duplicate_job(self) -> None:
        chat_id = -1001234567890
        await self.db.set_allowed_chat_id(chat_id)
        await self.db.update_settings(
            chat_id,
            ad_text="Тест",
            ad_interval_seconds=300,
            ad_enabled=True,
        )
        bot = Bot(DUMMY_TOKEN)
        scheduler = AdScheduler(bot, self.db)
        try:
            await scheduler.start()
            await scheduler.start()
            scheduler.schedule(chat_id, 600)
            scheduler.schedule(chat_id, 300)
            jobs = scheduler._scheduler.get_jobs()  # verifies the duplicate guard
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].id, f"advertisement:{chat_id}")
        finally:
            await scheduler.shutdown()
            await bot.session.close()


class UtilityTests(unittest.TestCase):
    def test_duration_parser(self) -> None:
        self.assertEqual(parse_duration("10м"), timedelta(minutes=10))
        self.assertEqual(parse_duration("2ч"), timedelta(hours=2))
        self.assertEqual(parse_duration("1д"), timedelta(days=1))
        self.assertIsNone(parse_duration("1минута"))
        self.assertIsNone(parse_duration("0м"))
        self.assertIsNone(parse_duration("366д"))

    def test_welcome_escapes_user_data_but_keeps_template_html(self) -> None:
        user = User(id=42, is_bot=False, first_name="<Admin>", username="name")
        result = render_welcome("<b>{name}</b> {mention}", user)
        self.assertIn("<b>&lt;Admin&gt;</b>", result)
        self.assertIn('href="tg://user?id=42"', result)


if __name__ == "__main__":
    unittest.main()
