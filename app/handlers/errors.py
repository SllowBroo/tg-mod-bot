from __future__ import annotations

import logging

from aiogram import Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ErrorEvent, Message

from app.filters import IsBangCommand

logger = logging.getLogger(__name__)
router = Router(name="errors_and_fallback")


@router.message(IsBangCommand())
async def unknown_command(message: Message) -> None:
    await message.answer("Неизвестная команда или неверный формат.")


@router.error()
async def global_error_handler(event: ErrorEvent) -> bool:
    update_id = getattr(event.update, "update_id", "unknown")
    if isinstance(event.exception, TelegramAPIError):
        logger.warning(
            "Ошибка Telegram API при обработке update_id=%s: %s",
            update_id,
            type(event.exception).__name__,
        )
    else:
        exception = event.exception
        logger.exception(
            "Необработанная ошибка при update_id=%s",
            update_id,
            exc_info=(type(exception), exception, exception.__traceback__),
        )
    return True
