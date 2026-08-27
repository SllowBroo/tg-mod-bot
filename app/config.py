from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True, slots=True)
class Config:
    bot_token: str
    database_path: Path
    allowed_chat_id: int | None
    log_level: str
    command_cooldown: float


def load_config() -> Config:
    load_dotenv(BASE_DIR / ".env")

    token = os.getenv("BOT_TOKEN", "").strip()
    if not token or token == "ВСТАВИТЬ_НОВЫЙ_ТОКЕН_ЗДЕСЬ":
        raise RuntimeError(
            "Переменная BOT_TOKEN не задана или содержит шаблонное значение"
        )

    database_value = (
        os.getenv("DATABASE_PATH")
        or os.getenv("DB_PATH")
        or str(BASE_DIR / "data" / "bot.db")
    )
    database_path = Path(database_value).expanduser().resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)

    chat_value = os.getenv("ALLOWED_CHAT_ID", "").strip()
    try:
        allowed_chat_id = int(chat_value) if chat_value else None
    except ValueError as exc:
        raise RuntimeError("ALLOWED_CHAT_ID должен быть целым числом") from exc

    try:
        cooldown = max(0.2, float(os.getenv("COMMAND_COOLDOWN", "1.0")))
    except ValueError as exc:
        raise RuntimeError("COMMAND_COOLDOWN должен быть числом") from exc

    return Config(
        bot_token=token,
        database_path=database_path,
        allowed_chat_id=allowed_chat_id,
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        command_cooldown=cooldown,
    )
