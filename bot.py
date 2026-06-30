# -*- coding: utf-8 -*-
"""
Телеграм-бот: модерация группы + авторассылка рекламы.
Все команды на русском, пишутся обычным текстом (не через "/").

Зависимости:  pip install aiogram
Запуск:        python bot.py
"""

import asyncio
import json
import logging
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import ChatPermissions, Message

# ----------------------------------------------------------------------------
# Настройки и пути
# ----------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DB_PATH = os.path.join(BASE_DIR, "bot.db")

WARNS_TO_BAN = 3  # сколько предупреждений до автобана

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("modbot")


# ----------------------------------------------------------------------------
# Работа с конфигом (токен + настройки рассылки сохраняются в config.json)
# ----------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "token": "",
    "broadcast": {
        "enabled": False,
        "text": "",
        "interval_seconds": 3600,
        "chat_id": None,
    },
}


def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        save_config(DEFAULT_CONFIG)
        return json.loads(json.dumps(DEFAULT_CONFIG))
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # дополняем недостающие поля
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    for k, v in DEFAULT_CONFIG["broadcast"].items():
        cfg["broadcast"].setdefault(k, v)
    return cfg


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ----------------------------------------------------------------------------
# База предупреждений (SQLite)
# ----------------------------------------------------------------------------
def init_db() -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS warns (
                   chat_id INTEGER,
                   user_id INTEGER,
                   count   INTEGER DEFAULT 0,
                   PRIMARY KEY (chat_id, user_id)
               )"""
        )
        conn.commit()


def get_warns(chat_id: int, user_id: int) -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT count FROM warns WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()
        return row[0] if row else 0


def add_warn(chat_id: int, user_id: int) -> int:
    count = get_warns(chat_id, user_id) + 1
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT INTO warns (chat_id, user_id, count) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET count=?",
            (chat_id, user_id, count, count),
        )
        conn.commit()
    return count


def reset_warns(chat_id: int, user_id: int) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "DELETE FROM warns WHERE chat_id=? AND user_id=?", (chat_id, user_id)
        )
        conn.commit()


def remove_one_warn(chat_id: int, user_id: int) -> int:
    count = max(0, get_warns(chat_id, user_id) - 1)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        if count == 0:
            conn.execute(
                "DELETE FROM warns WHERE chat_id=? AND user_id=?", (chat_id, user_id)
            )
        else:
            conn.execute(
                "UPDATE warns SET count=? WHERE chat_id=? AND user_id=?",
                (count, chat_id, user_id),
            )
        conn.commit()
    return count


# ----------------------------------------------------------------------------
# Вспомогательные функции
# ----------------------------------------------------------------------------
DURATION_UNITS = {
    "с": 1, "сек": 1, "секунд": 1, "c": 1, "s": 1,
    "м": 60, "мин": 60, "минут": 60, "m": 60,
    "ч": 3600, "час": 3600, "часов": 3600, "h": 3600,
    "д": 86400, "дн": 86400, "день": 86400, "дней": 86400, "d": 86400,
}


def parse_duration(token: str):
    """ '30м', '2ч', '1д', '90с' -> секунды. None если не распознано. """
    if not token:
        return None
    m = re.fullmatch(r"(\d+)\s*([а-яёa-z]*)", token.strip(), re.IGNORECASE)
    if not m:
        return None
    value = int(m.group(1))
    unit = m.group(2).lower() or "м"  # без единицы — считаем минутами
    mult = DURATION_UNITS.get(unit)
    if mult is None:
        return None
    return value * mult


def human_duration(seconds: int) -> str:
    if seconds <= 0:
        return "навсегда"
    parts = []
    for label, size in (("д", 86400), ("ч", 3600), ("м", 60), ("с", 1)):
        if seconds >= size:
            parts.append(f"{seconds // size}{label}")
            seconds %= size
    return " ".join(parts)


def mention(user) -> str:
    """ Ссылка-упоминание пользователя. """
    name = (user.full_name or "пользователь").replace("<", "&lt;").replace(">", "&gt;")
    return f'<a href="tg://user?id={user.id}">{name}</a>'


def strip_command(text: str, cmd_word: str) -> str:
    """ Убираем команду и необязательное слово 'причина'. """
    rest = text[len(cmd_word):].strip()
    rest = re.sub(r"^причина\s+", "", rest, flags=re.IGNORECASE)
    return rest.strip(" \"'")


async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except TelegramBadRequest:
        return False
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)


async def get_target(message: Message):
    """ Возвращает объект пользователя-цели: из reply или по числовому ID. """
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user
    # попытка взять числовой ID из текста
    m = re.search(r"\b(\d{5,})\b", message.text or "")
    if m:
        try:
            cm = await message.bot.get_chat_member(message.chat.id, int(m.group(1)))
            return cm.user
        except TelegramBadRequest:
            return None
    return None


async def guard(message: Message) -> bool:
    """ Проверка: команда в группе + автор админ. Иначе сообщает и возвращает False. """
    if message.chat.type not in ("group", "supergroup"):
        await message.reply("⚠️ Эта команда работает только в группе.")
        return False
    if not await is_admin(message.bot, message.chat.id, message.from_user.id):
        await message.reply("🚫 Команда доступна только администраторам.")
        return False
    return True


# Разрешения для размута (полный доступ к чату)
FULL_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_invite_users=True,
)
MUTE_PERMISSIONS = ChatPermissions(can_send_messages=False)


# ----------------------------------------------------------------------------
# Менеджер авторассылки
# ----------------------------------------------------------------------------
class Broadcaster:
    def __init__(self, bot: Bot, cfg: dict):
        self.bot = bot
        self.cfg = cfg
        self.task: asyncio.Task | None = None

    @property
    def settings(self) -> dict:
        return self.cfg["broadcast"]

    def save(self):
        save_config(self.cfg)

    async def _loop(self):
        try:
            while True:
                interval = max(10, int(self.settings["interval_seconds"]))
                await asyncio.sleep(interval)
                text = self.settings.get("text")
                chat_id = self.settings.get("chat_id")
                if text and chat_id:
                    try:
                        await self.bot.send_message(chat_id, text)
                        log.info("Реклама отправлена в %s", chat_id)
                    except Exception as e:  # noqa: BLE001
                        log.warning("Не удалось отправить рекламу: %s", e)
        except asyncio.CancelledError:
            pass

    def start(self):
        self.stop()
        self.settings["enabled"] = True
        self.save()
        self.task = asyncio.create_task(self._loop())

    def stop(self):
        if self.task and not self.task.done():
            self.task.cancel()
        self.task = None

    def disable(self):
        self.stop()
        self.settings["enabled"] = False
        self.save()


# ----------------------------------------------------------------------------
# Регистрация хендлеров
# ----------------------------------------------------------------------------
def cmd_filter(*words):
    """ Фильтр текстовых команд (без учёта регистра), команда в начале строки. """
    pattern = re.compile(r"(?i)^(" + "|".join(words) + r")(\s|$)")
    return F.text.func(lambda t: bool(t and pattern.match(t)))


def matched_word(text: str, words) -> str:
    for w in words:
        if re.match(rf"(?i)^{w}(\s|$)", text):
            return text[:len(w)]
    return words[0]


def build_dispatcher(bot: Bot, cfg: dict) -> tuple[Dispatcher, Broadcaster]:
    dp = Dispatcher()
    bc = Broadcaster(bot, cfg)

    # ----- ПОМОЩЬ -----
    @dp.message(cmd_filter("помощь", "команды", "хелп"))
    async def help_cmd(message: Message):
        await message.reply(HELP_TEXT)

    # ----- БАН -----
    @dp.message(cmd_filter("бан", "забанить"))
    async def ban_cmd(message: Message):
        if not await guard(message):
            return
        target = await get_target(message)
        if not target:
            await message.reply("ℹ️ Ответьте на сообщение пользователя или укажите его ID.")
            return
        reason = strip_command(message.text, matched_word(message.text, ["забанить", "бан"])) or "не указана"
        try:
            await bot.ban_chat_member(message.chat.id, target.id)
            reset_warns(message.chat.id, target.id)
            await message.answer(f"🔨 {mention(target)} забанен.\nПричина: <b>{reason}</b>")
        except TelegramBadRequest as e:
            await message.reply(f"Не удалось забанить: {e.message}")

    # ----- РАЗБАН -----
    @dp.message(cmd_filter("разбан", "разбанить"))
    async def unban_cmd(message: Message):
        if not await guard(message):
            return
        target = await get_target(message)
        if not target:
            await message.reply("ℹ️ Ответьте на сообщение пользователя или укажите его ID.")
            return
        try:
            await bot.unban_chat_member(message.chat.id, target.id, only_if_banned=True)
            await message.answer(f"✅ {mention(target)} разбанен.")
        except TelegramBadRequest as e:
            await message.reply(f"Не удалось разбанить: {e.message}")

    # ----- КИК (исключить) -----
    @dp.message(cmd_filter("кик", "кикнуть", "выгнать"))
    async def kick_cmd(message: Message):
        if not await guard(message):
            return
        target = await get_target(message)
        if not target:
            await message.reply("ℹ️ Ответьте на сообщение пользователя или укажите его ID.")
            return
        reason = strip_command(message.text, matched_word(message.text, ["кикнуть", "выгнать", "кик"])) or "не указана"
        try:
            # бан + сразу разбан = исключение без блокировки повторного входа
            await bot.ban_chat_member(message.chat.id, target.id)
            await bot.unban_chat_member(message.chat.id, target.id)
            await message.answer(f"👋 {mention(target)} исключён.\nПричина: <b>{reason}</b>")
        except TelegramBadRequest as e:
            await message.reply(f"Не удалось исключить: {e.message}")

    # ----- МУТ -----
    @dp.message(cmd_filter("мут", "замутить"))
    async def mute_cmd(message: Message):
        if not await guard(message):
            return
        target = await get_target(message)
        if not target:
            await message.reply("ℹ️ Ответьте на сообщение пользователя.\nПример: <code>Мут 1ч причина спам</code>")
            return
        word = matched_word(message.text, ["замутить", "мут"])
        rest = strip_command(message.text, word)
        # первый токен может быть длительностью
        tokens = rest.split(maxsplit=1)
        seconds = None
        reason = rest
        if tokens:
            dur = parse_duration(tokens[0])
            if dur is not None:
                seconds = dur
                reason = tokens[1] if len(tokens) > 1 else ""
        reason = re.sub(r"^причина\s+", "", reason, flags=re.IGNORECASE).strip(" \"'") or "не указана"
        until = None
        if seconds:
            until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        try:
            await bot.restrict_chat_member(
                message.chat.id, target.id,
                permissions=MUTE_PERMISSIONS, until_date=until,
            )
            srok = human_duration(seconds or 0)
            await message.answer(
                f"🔇 {mention(target)} в муте.\nСрок: <b>{srok}</b>\nПричина: <b>{reason}</b>"
            )
        except TelegramBadRequest as e:
            await message.reply(f"Не удалось замутить: {e.message}")

    # ----- РАЗМУТ -----
    @dp.message(cmd_filter("размут", "размутить"))
    async def unmute_cmd(message: Message):
        if not await guard(message):
            return
        target = await get_target(message)
        if not target:
            await message.reply("ℹ️ Ответьте на сообщение пользователя.")
            return
        try:
            await bot.restrict_chat_member(
                message.chat.id, target.id, permissions=FULL_PERMISSIONS
            )
            await message.answer(f"🔊 {mention(target)} размучен.")
        except TelegramBadRequest as e:
            await message.reply(f"Не удалось размутить: {e.message}")

    # ----- ВАРН (предупреждение) -----
    @dp.message(cmd_filter("варн", "пред", "предупреждение"))
    async def warn_cmd(message: Message):
        if not await guard(message):
            return
        target = await get_target(message)
        if not target:
            await message.reply("ℹ️ Ответьте на сообщение пользователя.")
            return
        word = matched_word(message.text, ["предупреждение", "варн", "пред"])
        reason = strip_command(message.text, word) or "не указана"
        count = add_warn(message.chat.id, target.id)
        if count >= WARNS_TO_BAN:
            try:
                await bot.ban_chat_member(message.chat.id, target.id)
                reset_warns(message.chat.id, target.id)
                await message.answer(
                    f"⚠️ {mention(target)} получил {count}/{WARNS_TO_BAN} предупреждений "
                    f"и был автоматически забанен.\nПричина: <b>{reason}</b>"
                )
            except TelegramBadRequest as e:
                await message.reply(f"Достигнут лимит варнов, но забанить не вышло: {e.message}")
        else:
            await message.answer(
                f"⚠️ {mention(target)} — предупреждение {count}/{WARNS_TO_BAN}.\n"
                f"Причина: <b>{reason}</b>"
            )

    # ----- СНЯТЬ ВАРН -----
    @dp.message(cmd_filter("снять варн", "снятьварн", "снять пред"))
    async def unwarn_cmd(message: Message):
        if not await guard(message):
            return
        target = await get_target(message)
        if not target:
            await message.reply("ℹ️ Ответьте на сообщение пользователя.")
            return
        count = remove_one_warn(message.chat.id, target.id)
        await message.answer(f"➖ С {mention(target)} снято предупреждение. Осталось: {count}/{WARNS_TO_BAN}.")

    # ----- ВАРНЫ (посмотреть) -----
    @dp.message(cmd_filter("варны", "преды"))
    async def warns_cmd(message: Message):
        if not await guard(message):
            return
        target = await get_target(message)
        if not target:
            await message.reply("ℹ️ Ответьте на сообщение пользователя.")
            return
        count = get_warns(message.chat.id, target.id)
        await message.answer(f"📋 У {mention(target)}: {count}/{WARNS_TO_BAN} предупреждений.")

    # ----- ИНФО -----
    @dp.message(cmd_filter("инфо", "id", "айди"))
    async def info_cmd(message: Message):
        target = await get_target(message) or message.from_user
        count = get_warns(message.chat.id, target.id)
        await message.reply(
            f"👤 {mention(target)}\n"
            f"ID: <code>{target.id}</code>\n"
            f"Юзернейм: @{target.username if target.username else '—'}\n"
            f"Предупреждений: {count}/{WARNS_TO_BAN}"
        )

    # ----- РЕКЛАМА (управление рассылкой) -----
    @dp.message(cmd_filter("реклама", "рассылка"))
    async def ad_cmd(message: Message):
        if not await guard(message):
            return
        word = matched_word(message.text, ["рассылка", "реклама"])
        rest = message.text[len(word):].strip()
        low = rest.lower()
        s = bc.settings

        if low.startswith("текст"):
            new_text = rest[len("текст"):].strip()
            if not new_text:
                await message.reply("Укажите текст: <code>Реклама текст Ваше объявление</code>")
                return
            s["text"] = new_text
            bc.save()
            await message.reply("✅ Текст рекламы сохранён.")

        elif low.startswith("интервал"):
            arg = rest[len("интервал"):].strip()
            seconds = parse_duration(arg)
            if not seconds:
                await message.reply("Укажите интервал: <code>Реклама интервал 2ч</code> (с/м/ч/д)")
                return
            s["interval_seconds"] = seconds
            bc.save()
            if s.get("enabled"):
                bc.start()  # перезапуск с новым интервалом
            await message.reply(f"✅ Интервал рассылки: каждые {human_duration(seconds)}.")

        elif low.startswith("старт") or low.startswith("вкл"):
            if not s.get("text"):
                await message.reply("Сначала задайте текст: <code>Реклама текст ...</code>")
                return
            s["chat_id"] = message.chat.id
            bc.start()
            await message.reply(
                f"🚀 Рассылка запущена в этой группе.\nИнтервал: {human_duration(s['interval_seconds'])}."
            )

        elif low.startswith("стоп") or low.startswith("выкл"):
            bc.disable()
            await message.reply("🛑 Рассылка остановлена.")

        elif low.startswith("статус"):
            state = "включена ✅" if s.get("enabled") else "выключена ❌"
            preview = (s.get("text") or "—")
            if len(preview) > 200:
                preview = preview[:200] + "…"
            await message.reply(
                f"📣 <b>Рассылка</b>: {state}\n"
                f"Интервал: {human_duration(s['interval_seconds'])}\n"
                f"Целевая группа: <code>{s.get('chat_id') or '—'}</code>\n"
                f"Текст:\n{preview}"
            )

        elif low.startswith("отправить") or low.startswith("сейчас"):
            if s.get("text"):
                await bot.send_message(message.chat.id, s["text"])
            else:
                await message.reply("Текст рекламы не задан.")
        else:
            await message.reply(AD_HELP)

    return dp, bc


HELP_TEXT = (
    "🤖 <b>Команды бота</b> (пишутся текстом, ответом на сообщение пользователя)\n\n"
    "<b>Модерация:</b>\n"
    "• <code>Бан [причина]</code> — забанить\n"
    "• <code>Разбан</code> — разбанить\n"
    "• <code>Кик [причина]</code> — исключить (без блокировки)\n"
    "• <code>Мут 1ч [причина]</code> — мут на срок (с/м/ч/д)\n"
    "• <code>Размут</code> — снять мут\n"
    "• <code>Варн [причина]</code> — предупреждение (3 = автобан)\n"
    "• <code>Снять варн</code> — убрать одно предупреждение\n"
    "• <code>Варны</code> — сколько предупреждений\n"
    "• <code>Инфо</code> — данные пользователя\n\n"
    + "<b>Рассылка рекламы:</b>\n" + (
        "• <code>Реклама текст ...</code> — задать текст\n"
        "• <code>Реклама интервал 2ч</code> — задать интервал\n"
        "• <code>Реклама старт</code> / <code>Реклама стоп</code>\n"
        "• <code>Реклама статус</code> — текущие настройки\n"
        "• <code>Реклама отправить</code> — отправить сейчас\n"
    )
)

AD_HELP = (
    "📣 <b>Управление рассылкой:</b>\n"
    "• <code>Реклама текст ...</code>\n"
    "• <code>Реклама интервал 2ч</code>\n"
    "• <code>Реклама старт</code> / <code>Реклама стоп</code>\n"
    "• <code>Реклама статус</code>\n"
    "• <code>Реклама отправить</code> — прямо сейчас"
)


# ----------------------------------------------------------------------------
# Точка входа
# ----------------------------------------------------------------------------
async def main():
    init_db()
    cfg = load_config()

    token = os.environ.get("BOT_TOKEN") or cfg.get("token")
    if not token:
        raise SystemExit(
            "❌ Не задан токен бота. Впишите его в config.json (поле \"token\")\n"
            "   или задайте переменную окружения BOT_TOKEN."
        )

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp, bc = build_dispatcher(bot, cfg)

    # если рассылка была включена ранее — поднимаем её
    if cfg["broadcast"].get("enabled") and cfg["broadcast"].get("chat_id"):
        bc.start()

    log.info("Бот запущен.")
    try:
        await dp.start_polling(bot)
    finally:
        bc.stop()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
