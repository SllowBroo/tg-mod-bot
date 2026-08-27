from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import aiosqlite


@dataclass(slots=True)
class ChatSettings:
    chat_id: int
    ad_text: str | None
    ad_photo_file_id: str | None
    ad_caption: str | None
    ad_interval_seconds: int
    ad_enabled: bool
    welcome_text: str | None
    welcome_enabled: bool


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[aiosqlite.Connection]:
        connection = await aiosqlite.connect(self.path)
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA foreign_keys = ON")
        await connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
        finally:
            await connection.close()

    async def initialize(self) -> None:
        async with self.connect() as db:
            await db.execute("PRAGMA journal_mode = WAL")
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS app_config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS chat_settings (
                    chat_id INTEGER PRIMARY KEY,
                    ad_text TEXT,
                    ad_photo_file_id TEXT,
                    ad_caption TEXT,
                    ad_interval_seconds INTEGER NOT NULL DEFAULT 3600,
                    ad_enabled INTEGER NOT NULL DEFAULT 0,
                    welcome_text TEXT,
                    welcome_enabled INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS quick_replies (
                    chat_id INTEGER NOT NULL,
                    reply_key TEXT NOT NULL,
                    text TEXT NOT NULL,
                    PRIMARY KEY (chat_id, reply_key)
                );

                CREATE TABLE IF NOT EXISTS warnings (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (chat_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS known_users (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    full_name TEXT NOT NULL,
                    PRIMARY KEY (chat_id, user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_known_users_username
                    ON known_users(chat_id, username COLLATE NOCASE);
                """
            )
            await db.commit()

    async def get_allowed_chat_id(self) -> int | None:
        async with self.connect() as db:
            cursor = await db.execute(
                "SELECT value FROM app_config WHERE key = 'allowed_chat_id'"
            )
            row = await cursor.fetchone()
        return int(row["value"]) if row else None

    async def set_allowed_chat_id(self, chat_id: int) -> None:
        async with self.connect() as db:
            await db.execute(
                """
                INSERT INTO app_config(key, value) VALUES('allowed_chat_id', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(chat_id),),
            )
            await self._ensure_chat(db, chat_id)
            await db.commit()

    async def _ensure_chat(self, db: aiosqlite.Connection, chat_id: int) -> None:
        await db.execute(
            "INSERT OR IGNORE INTO chat_settings(chat_id) VALUES(?)", (chat_id,)
        )

    async def get_settings(self, chat_id: int) -> ChatSettings:
        async with self.connect() as db:
            await self._ensure_chat(db, chat_id)
            await db.commit()
            cursor = await db.execute(
                "SELECT * FROM chat_settings WHERE chat_id = ?", (chat_id,)
            )
            row = await cursor.fetchone()
        assert row is not None
        return ChatSettings(
            chat_id=row["chat_id"],
            ad_text=row["ad_text"],
            ad_photo_file_id=row["ad_photo_file_id"],
            ad_caption=row["ad_caption"],
            ad_interval_seconds=row["ad_interval_seconds"],
            ad_enabled=bool(row["ad_enabled"]),
            welcome_text=row["welcome_text"],
            welcome_enabled=bool(row["welcome_enabled"]),
        )

    async def update_settings(self, chat_id: int, **values: object) -> None:
        allowed = {
            "ad_text",
            "ad_photo_file_id",
            "ad_caption",
            "ad_interval_seconds",
            "ad_enabled",
            "welcome_text",
            "welcome_enabled",
        }
        if not values or not set(values).issubset(allowed):
            raise ValueError("Недопустимое поле настроек")
        assignments = ", ".join(f"{key} = ?" for key in values)
        params = [
            int(value) if isinstance(value, bool) else value
            for value in values.values()
        ]
        async with self.connect() as db:
            await self._ensure_chat(db, chat_id)
            await db.execute(
                f"UPDATE chat_settings SET {assignments} WHERE chat_id = ?",
                (*params, chat_id),
            )
            await db.commit()

    async def get_enabled_ad_chats(self) -> list[int]:
        async with self.connect() as db:
            cursor = await db.execute(
                "SELECT chat_id FROM chat_settings WHERE ad_enabled = 1"
            )
            rows = await cursor.fetchall()
        return [row["chat_id"] for row in rows]

    async def set_quick_reply(self, chat_id: int, key: str, text: str) -> None:
        async with self.connect() as db:
            await db.execute(
                """
                INSERT INTO quick_replies(chat_id, reply_key, text) VALUES(?, ?, ?)
                ON CONFLICT(chat_id, reply_key) DO UPDATE SET text = excluded.text
                """,
                (chat_id, key, text),
            )
            await db.commit()

    async def get_quick_reply(self, chat_id: int, key: str) -> str | None:
        async with self.connect() as db:
            cursor = await db.execute(
                "SELECT text FROM quick_replies WHERE chat_id = ? AND reply_key = ?",
                (chat_id, key),
            )
            row = await cursor.fetchone()
        return row["text"] if row else None

    async def remember_user(
        self, chat_id: int, user_id: int, username: str | None, full_name: str
    ) -> None:
        username_normalized = username.lower() if username else None
        async with self.connect() as db:
            await db.execute(
                """
                INSERT INTO known_users(chat_id, user_id, username, full_name)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET
                    username = excluded.username,
                    full_name = excluded.full_name
                """,
                (chat_id, user_id, username_normalized, full_name),
            )
            await db.commit()

    async def find_user_by_username(self, chat_id: int, username: str) -> int | None:
        async with self.connect() as db:
            cursor = await db.execute(
                """
                SELECT user_id FROM known_users
                WHERE chat_id = ? AND username = ? COLLATE NOCASE
                """,
                (chat_id, username.lstrip("@").lower()),
            )
            row = await cursor.fetchone()
        return row["user_id"] if row else None

    async def change_warning(self, chat_id: int, user_id: int, delta: int) -> int:
        async with self.connect() as db:
            await db.execute(
                "INSERT OR IGNORE INTO warnings(chat_id, user_id, count) VALUES(?, ?, 0)",
                (chat_id, user_id),
            )
            await db.execute(
                """
                UPDATE warnings SET count = MAX(0, count + ?)
                WHERE chat_id = ? AND user_id = ?
                """,
                (delta, chat_id, user_id),
            )
            cursor = await db.execute(
                "SELECT count FROM warnings WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            )
            row = await cursor.fetchone()
            await db.commit()
        return int(row["count"]) if row else 0
