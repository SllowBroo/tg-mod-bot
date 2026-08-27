from __future__ import annotations

import html

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ChatPermissions, Message

from app.database import Database
from app.filters import BangCommand
from app.utils import (
    bot_can_restrict,
    ensure_allowed_chat,
    parse_duration,
    require_admin,
    resolve_target,
    target_is_protected,
    until_date,
    user_link,
)

router = Router(name="moderation")


async def _prepare_admin_command(message: Message, bot: Bot, db: Database) -> bool:
    return await require_admin(message, bot) and await ensure_allowed_chat(
        message, db, bind_if_empty=True
    )


@router.message(BangCommand("бан"))
async def ban_user(message: Message, command_args: str, bot: Bot, db: Database) -> None:
    if not await _prepare_admin_command(message, bot, db) or not await bot_can_restrict(
        message, bot
    ):
        return
    target = await resolve_target(message, db, command_args)
    if not target or await target_is_protected(message, bot, target[0]):
        return
    try:
        await bot.ban_chat_member(message.chat.id, target[0])
        await message.answer(f"Пользователь {user_link(*target)} заблокирован.")
    except TelegramAPIError:
        await message.answer(
            "Telegram не разрешил заблокировать пользователя. Проверьте права бота."
        )


@router.message(BangCommand("разбан"))
async def unban_user(
    message: Message, command_args: str, bot: Bot, db: Database
) -> None:
    if not await _prepare_admin_command(message, bot, db) or not await bot_can_restrict(
        message, bot
    ):
        return
    target = await resolve_target(message, db, command_args)
    if not target:
        return
    try:
        await bot.unban_chat_member(message.chat.id, target[0], only_if_banned=True)
        await message.answer(f"Пользователь {user_link(*target)} разблокирован.")
    except TelegramAPIError:
        await message.answer("Telegram не разрешил разблокировать пользователя.")


def _mute_parts(message: Message, args: str) -> tuple[str | None, str | None]:
    parts = args.split()
    if not parts:
        return None, None
    if parse_duration(parts[0]):
        return parts[0], parts[1] if len(parts) > 1 else None
    if len(parts) > 1 and parse_duration(parts[1]):
        return parts[1], parts[0]
    return None, None


@router.message(BangCommand("мут"))
async def mute_user(
    message: Message, command_args: str, bot: Bot, db: Database
) -> None:
    if not await _prepare_admin_command(message, bot, db) or not await bot_can_restrict(
        message, bot
    ):
        return
    duration_text, target_arg = _mute_parts(message, command_args)
    duration = parse_duration(duration_text or "")
    if duration is None:
        await message.answer(
            "Использование: ответьте <code>!мут 10м</code> или укажите "
            "<code>!мут 10м @username</code>. Единицы: м, ч, д; максимум 365д."
        )
        return
    target = await resolve_target(message, db, target_arg)
    if not target or await target_is_protected(message, bot, target[0]):
        return
    try:
        await bot.restrict_chat_member(
            message.chat.id,
            target[0],
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until_date(duration),
        )
        await message.answer(f"Пользователь {user_link(*target)} временно ограничен.")
    except TelegramAPIError:
        await message.answer(
            "Telegram не разрешил ограничить пользователя. Проверьте права бота."
        )


@router.message(BangCommand("размут"))
async def unmute_user(
    message: Message, command_args: str, bot: Bot, db: Database
) -> None:
    if not await _prepare_admin_command(message, bot, db) or not await bot_can_restrict(
        message, bot
    ):
        return
    target = await resolve_target(message, db, command_args)
    if not target or await target_is_protected(message, bot, target[0]):
        return
    try:
        chat = await bot.get_chat(message.chat.id)
        permissions = chat.permissions or ChatPermissions(can_send_messages=True)
        await bot.restrict_chat_member(
            message.chat.id, target[0], permissions=permissions
        )
        await message.answer(f"Ограничения для {user_link(*target)} сняты.")
    except TelegramAPIError:
        await message.answer("Telegram не разрешил снять ограничения.")


@router.message(BangCommand("предупреждение"))
async def warn_user(
    message: Message, command_args: str, bot: Bot, db: Database
) -> None:
    if not await _prepare_admin_command(message, bot, db) or not await bot_can_restrict(
        message, bot
    ):
        return
    target = await resolve_target(message, db, command_args)
    if not target or await target_is_protected(message, bot, target[0]):
        return
    count = await db.change_warning(message.chat.id, target[0], 1)
    if count >= 3:
        try:
            await bot.ban_chat_member(message.chat.id, target[0])
            await message.answer(
                f"{user_link(*target)} получил 3 предупреждения и автоматически заблокирован."
            )
        except TelegramAPIError:
            await message.answer(
                f"У пользователя {user_link(*target)} уже {count} предупреждения, "
                "но Telegram не разрешил автоблокировку."
            )
        return
    await message.answer(f"{user_link(*target)}: предупреждений {count}/3.")


@router.message(BangCommand("снятьпредупреждение"))
async def remove_warning(
    message: Message, command_args: str, bot: Bot, db: Database
) -> None:
    if not await _prepare_admin_command(message, bot, db):
        return
    target = await resolve_target(message, db, command_args)
    if not target:
        return
    count = await db.change_warning(message.chat.id, target[0], -1)
    await message.answer(f"{user_link(*target)}: предупреждений {count}/3.")


@router.message(BangCommand("админы", exact=True))
async def list_admins(message: Message, bot: Bot, db: Database) -> None:
    if not await _prepare_admin_command(message, bot, db):
        return
    try:
        admins = await bot.get_chat_administrators(message.chat.id)
    except TelegramAPIError:
        await message.answer("Не удалось получить список администраторов.")
        return
    lines = ["Администраторы группы:"]
    for member in admins:
        if member.user.is_bot:
            suffix = " (бот)"
        else:
            suffix = ""
        lines.append(f"• {user_link(member.user.id, member.user.full_name)}{suffix}")
    await message.answer("\n".join(lines))


@router.message(BangCommand("идентификатор"))
async def identifiers(
    message: Message, command_args: str, bot: Bot, db: Database
) -> None:
    if not await _prepare_admin_command(message, bot, db):
        return
    lines = [
        f"ID группы: <code>{message.chat.id}</code>",
        f"Ваш ID: <code>{message.from_user.id}</code>" if message.from_user else "",
    ]
    if message.reply_to_message and message.reply_to_message.from_user:
        user = message.reply_to_message.from_user
        lines.append(
            f"ID пользователя {html.escape(user.full_name)}: <code>{user.id}</code>"
        )
    elif command_args:
        target = await resolve_target(message, db, command_args)
        if target:
            lines.append(f"ID пользователя: <code>{target[0]}</code>")
    await message.answer("\n".join(filter(None, lines)))
