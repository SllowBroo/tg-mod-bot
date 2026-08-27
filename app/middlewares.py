from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import Message, TelegramObject

from app.database import Database


class RememberUsersMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if (
            isinstance(event, Message)
            and event.from_user
            and event.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}
        ):
            db: Database = data["db"]
            await db.remember_user(
                event.chat.id,
                event.from_user.id,
                event.from_user.username,
                event.from_user.full_name,
            )
        return await handler(event, data)


class CommandRateLimitMiddleware(BaseMiddleware):
    def __init__(self, cooldown: float) -> None:
        self.cooldown = cooldown
        self._last_seen: dict[tuple[int, int], float] = defaultdict(float)
        self._last_notice: dict[tuple[int, int], float] = defaultdict(float)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or not event.text:
            return await handler(event, data)
        if not event.text.lstrip().startswith("!") or not event.from_user:
            return await handler(event, data)

        key = (event.chat.id, event.from_user.id)
        now = time.monotonic()
        elapsed = now - self._last_seen[key]
        if elapsed < self.cooldown:
            if now - self._last_notice[key] >= 5:
                self._last_notice[key] = now
                await event.answer("Слишком часто. Повторите команду через секунду.")
            return None

        self._last_seen[key] = now
        return await handler(event, data)
