from __future__ import annotations

import time

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ChatMemberUpdated, Message, User

from app.database import Database
from app.filters import BangCommand
from app.utils import ensure_allowed_chat, render_welcome, require_admin

router = Router(name="welcome")

# Telegram can deliver both a ``new_chat_members`` service message and a
# ``chat_member`` update for the same join.  Keep a short-lived in-process
# guard so the fallback does not send the greeting twice.
_recent_greetings: dict[tuple[int, int], float] = {}


async def _send_welcome(message: Message, user: User, bot: Bot, db: Database) -> None:
    settings = await db.get_settings(message.chat.id)
    if not settings.welcome_enabled or not settings.welcome_text or user.is_bot:
        return
    now = time.monotonic()
    key = (message.chat.id, user.id)
    previous = _recent_greetings.get(key)
    if previous is not None and now - previous < 60:
        return
    _recent_greetings[key] = now
    # Keep the guard bounded if the bot processes a very busy group.
    for old_key, timestamp in list(_recent_greetings.items()):
        if now - timestamp >= 300:
            _recent_greetings.pop(old_key, None)
    await db.remember_user(message.chat.id, user.id, user.username, user.full_name)
    try:
        await message.answer(
            render_welcome(settings.welcome_text, user), parse_mode="HTML"
        )
    except TelegramAPIError:
        # Do not log the original HTML: it may contain private links.
        await message.answer(
            "Не удалось отправить приветствие: проверьте HTML-разметку."
        )


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
    for user in message.new_chat_members:
        await _send_welcome(message, user, bot, db)


@router.chat_member()
async def greet_from_membership_update(
    event: ChatMemberUpdated, bot: Bot, db: Database
) -> None:
    """Fallback for groups where service join messages are hidden/deleted."""
    # Membership updates have no ``answer`` method, so perform the allow-list
    # check without trying to send a diagnostic reply.
    if await db.get_allowed_chat_id() != event.chat.id:
        return
    old_status = event.old_chat_member.status
    new_status = event.new_chat_member.status
    joined = old_status in {"left", "kicked"} and new_status in {
        "member",
        "restricted",
        "administrator",
        "creator",
    }
    if not joined:
        return
    settings = await db.get_settings(event.chat.id)
    user = event.new_chat_member.user
    if not settings.welcome_enabled or not settings.welcome_text or user.is_bot:
        return
    now = time.monotonic()
    key = (event.chat.id, user.id)
    previous = _recent_greetings.get(key)
    if previous is not None and now - previous < 60:
        return
    _recent_greetings[key] = now
    for old_key, timestamp in list(_recent_greetings.items()):
        if now - timestamp >= 300:
            _recent_greetings.pop(old_key, None)
    await db.remember_user(event.chat.id, user.id, user.username, user.full_name)
    try:
        await bot.send_message(
            event.chat.id,
            render_welcome(settings.welcome_text, user),
            parse_mode="HTML",
        )
    except TelegramAPIError:
        await bot.send_message(
            event.chat.id,
            "Не удалось отправить приветствие: проверьте HTML-разметку.",
        )
