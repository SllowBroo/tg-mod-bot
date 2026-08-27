from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

from app.database import Database
from app.filters import BangCommand
from app.utils import ensure_allowed_chat, render_welcome, require_admin

router = Router(name="welcome")


@router.message(BangCommand("приветствие"))
async def welcome_command(
    message: Message, command_args: str, bot: Bot, db: Database
) -> None:
    if not await require_admin(message, bot):
        return
    if not await ensure_allowed_chat(message, db, bind_if_empty=True):
        return

    args = command_args.strip()
    lowered = args.lower()
    if lowered == "вкл":
        settings = await db.get_settings(message.chat.id)
        if not settings.welcome_text:
            await message.answer("Сначала задайте текст приветствия.")
            return
        await db.update_settings(message.chat.id, welcome_enabled=True)
        await message.answer("Приветствие включено.")
        return
    if lowered == "выкл":
        await db.update_settings(message.chat.id, welcome_enabled=False)
        await message.answer("Приветствие выключено.")
        return
    if args:
        await db.update_settings(message.chat.id, welcome_text=args)
        await message.answer(
            "Текст приветствия сохранён. Доступны: "
            "<code>{name}</code>, <code>{username}</code>, <code>{mention}</code>, "
            "<code>{id}</code>."
        )
        return
    await message.answer(
        "Использование: <code>!приветствие &lt;HTML-текст&gt;</code>, "
        "<code>!приветствие вкл</code> или <code>!приветствие выкл</code>."
    )


@router.message(F.new_chat_members)
async def greet_new_members(message: Message, bot: Bot, db: Database) -> None:
    if not await ensure_allowed_chat(message, db):
        return
    settings = await db.get_settings(message.chat.id)
    if not settings.welcome_enabled or not settings.welcome_text:
        return
    me = await bot.get_me()
    for user in message.new_chat_members:
        await db.remember_user(message.chat.id, user.id, user.username, user.full_name)
        if user.id == me.id:
            continue
        try:
            await message.answer(
                render_welcome(settings.welcome_text, user), parse_mode="HTML"
            )
        except TelegramAPIError:
            # Не отправляем исходный HTML в логи или чат: он может содержать частные ссылки.
            await message.answer(
                "Не удалось отправить приветствие: проверьте HTML-разметку."
            )
