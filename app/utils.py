from __future__ import annotations

import html
import re
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import Message, User

from app.database import Database

GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}
ADMIN_STATUSES = {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}
TIME_PATTERN = re.compile(r"^(\d+)([мчд])$", re.IGNORECASE)


async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ADMIN_STATUSES
    except (TelegramBadRequest, TelegramForbiddenError):
        return False


async def require_admin(message: Message, bot: Bot) -> bool:
    if message.chat.type not in GROUP_TYPES:
        await message.answer("Эта команда работает только в группе.")
        return False
    if not message.from_user or not await is_admin(
        bot, message.chat.id, message.from_user.id
    ):
        await message.answer("Команда доступна только администраторам группы.")
        return False
    return True


async def ensure_allowed_chat(
    message: Message, db: Database, *, bind_if_empty: bool = False
) -> bool:
    allowed_chat_id = await db.get_allowed_chat_id()
    if allowed_chat_id is None and bind_if_empty:
        await db.set_allowed_chat_id(message.chat.id)
        return True
    if allowed_chat_id != message.chat.id:
        if message.text and message.text.startswith("!"):
            await message.answer("Бот настроен для другой группы.")
        return False
    return True


async def remember_message_user(message: Message, db: Database) -> None:
    if message.chat.type not in GROUP_TYPES or not message.from_user:
        return
    await db.remember_user(
        message.chat.id,
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name,
    )


def parse_duration(value: str) -> timedelta | None:
    match = TIME_PATTERN.fullmatch(value.strip())
    if not match:
        return None
    amount = int(match.group(1))
    if amount <= 0:
        return None
    unit = match.group(2).lower()
    duration = {
        "м": timedelta(minutes=amount),
        "ч": timedelta(hours=amount),
        "д": timedelta(days=amount),
    }[unit]
    if duration < timedelta(seconds=30) or duration > timedelta(days=365):
        return None
    return duration


def until_date(duration: timedelta) -> datetime:
    return datetime.now(UTC) + duration


def user_link(user_id: int, name: str) -> str:
    return f'<a href="tg://user?id={user_id}">{html.escape(name)}</a>'


async def resolve_target(
    message: Message, db: Database, argument: str | None
) -> tuple[int, str] | None:
    if message.reply_to_message and message.reply_to_message.from_user:
        user = message.reply_to_message.from_user
        return user.id, user.full_name

    candidate = (argument or "").strip().split(maxsplit=1)[0]
    if not candidate:
        await message.answer(
            "Ответьте на сообщение пользователя или укажите @username/ID."
        )
        return None

    if candidate.lstrip("-").isdigit():
        return int(candidate), candidate

    if candidate.startswith("@"):
        user_id = await db.find_user_by_username(message.chat.id, candidate)
        if user_id is None:
            await message.answer(
                "Этот username ещё не встречался боту. Попросите пользователя написать "
                "сообщение или примените команду ответом на его сообщение."
            )
            return None
        return user_id, candidate

    await message.answer("Укажите @username/ID или ответьте на сообщение пользователя.")
    return None


async def target_is_protected(message: Message, bot: Bot, user_id: int) -> bool:
    me = await bot.get_me()
    if user_id == me.id:
        await message.answer("Нельзя применить эту команду к боту.")
        return True
    try:
        member = await bot.get_chat_member(message.chat.id, user_id)
    except TelegramBadRequest:
        return False
    if member.status in ADMIN_STATUSES:
        await message.answer("Нельзя применить эту команду к администратору.")
        return True
    return False


async def bot_can_restrict(message: Message, bot: Bot) -> bool:
    me = await bot.get_me()
    try:
        member = await bot.get_chat_member(message.chat.id, me.id)
    except (TelegramBadRequest, TelegramForbiddenError):
        await message.answer("Не удалось проверить права бота.")
        return False
    if member.status != ChatMemberStatus.ADMINISTRATOR or not getattr(
        member, "can_restrict_members", False
    ):
        await message.answer(
            "Боту нужны права администратора «Блокировка пользователей»."
        )
        return False
    return True


def render_welcome(template: str, user: User) -> str:
    username = f"@{user.username}" if user.username else user.full_name
    replacements = {
        "{name}": html.escape(user.full_name),
        "{username}": html.escape(username),
        "{id}": str(user.id),
        "{mention}": user_link(user.id, user.full_name),
    }
    result = template
    for key, value in replacements.items():
        result = result.replace(key, value)
    return result
