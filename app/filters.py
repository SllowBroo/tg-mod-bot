from __future__ import annotations

from dataclasses import dataclass

from aiogram.filters import BaseFilter
from aiogram.types import Message


@dataclass(frozen=True, slots=True)
class BangCommand(BaseFilter):
    command: str
    exact: bool = False

    async def __call__(self, message: Message) -> bool | dict[str, str]:
        text = (message.text or message.caption or "").strip()
        expected = f"!{self.command}".lower()
        lowered = text.lower()
        if self.exact:
            return lowered == expected
        if lowered == expected:
            return {"command_args": ""}
        if lowered.startswith(expected + " "):
            return {"command_args": text[len(expected) :].strip()}
        return False


class IsBangCommand(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return bool(message.text and message.text.lstrip().startswith("!"))
