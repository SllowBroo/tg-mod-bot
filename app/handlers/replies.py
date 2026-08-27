from __future__ import annotations

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

from app.database import Database
from app.filters import BangCommand
from app.utils import ensure_allowed_chat, require_admin

router = Router(name="quick_replies")
KEYS = {"купить дзо": "buy_dzo", "купить мс": "buy_ms"}


@router.message(BangCommand("текст"))
async def set_quick_reply(
    message: Message, command_args: str, bot: Bot, db: Database
) -> None:
    if not await require_admin(message, bot):
        return
    if not await ensure_allowed_chat(message, db, bind_if_empty=True):
        return

    lowered = command_args.lower()
    for prefix, key in KEYS.items():
        marker = prefix + " "
        if lowered.startswith(marker):
            text = command_args[len(marker) :].strip()
            if not text:
                break
            await db.set_quick_reply(message.chat.id, key, text)
            await message.answer(f"Быстрый ответ «{prefix}» сохранён.")
            return
    await message.answer(
        "Использование: <code>!текст купить дзо &lt;текст&gt;</code> или "
        "<code>!текст купить мс &lt;текст&gt;</code>."
    )


async def _send_reply(message: Message, db: Database, key: str) -> None:
    if not await ensure_allowed_chat(message, db):
        return
    text = await db.get_quick_reply(message.chat.id, key)
    if not text:
        await message.answer("Ответ ещё не настроен администратором.")
        return
    try:
        await message.answer(text, parse_mode="HTML", disable_web_page_preview=False)
    except TelegramAPIError:
        await message.answer(
            "Не удалось отправить ответ: администратору нужно проверить HTML."
        )


@router.message(BangCommand("купить дзо", exact=True))
async def buy_dzo(message: Message, db: Database) -> None:
    await _send_reply(message, db, "buy_dzo")


@router.message(BangCommand("купить мс", exact=True))
async def buy_ms(message: Message, db: Database) -> None:
    await _send_reply(message, db, "buy_ms")
