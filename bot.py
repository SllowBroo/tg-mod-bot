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
import random
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import JOIN_TRANSITION, ChatMemberUpdatedFilter, CommandStart
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ----------------------------------------------------------------------------
# Настройки и пути
# ----------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DB_PATH = os.path.join(BASE_DIR, "bot.db")

WARNS_TO_BAN = 3  # сколько предупреждений до автобана

# ----------------------------------------------------------------------------
# Экономика DZO
# ----------------------------------------------------------------------------
# Иконка валюты. В сообщениях (parse_mode=HTML) идёт кастомный эмодзи монеты
# из набора DzoFun, на кнопках с ценой та же монета иконкой. Где кастомный
# эмодзи невозможен (всплывающие окна, кнопки без иконки, запасной путь при
# отказе Telegram), идёт жёлтый кружок 🟡: обычная 🪙 на телефонах серая.
# Вернуть обычный эмодзи везде: USE_CUSTOM_EMOJI = False.
USE_CUSTOM_EMOJI = True
DZO_EMOJI_ID = "5377350967861748561"
DZO_EMOJI_PLAIN = "🟡"
# Замещающий символ внутри <tg-emoji> (его видно только там, где Telegram
# не рисует кастомный эмодзи, например в тексте уведомления). Оставлен тем же,
# с которым Telegram принимает эту монету с 11.10.
DZO_EMOJI_ALT = "🪙"
DZO_EMOJI = (
    f'<tg-emoji emoji-id="{DZO_EMOJI_ID}">{DZO_EMOJI_ALT}</tg-emoji>'
    if USE_CUSTOM_EMOJI else DZO_EMOJI_PLAIN
)
CURRENCY = "DZO"             # название валюты в текстах
FARM_COOLDOWN = 4 * 3600     # фарм доступен раз в 4 часа
FARM_MIN, FARM_MAX = 1, 3    # диапазон выпадения DZO (равные шансы)
WITHDRAW_MIN = 1000          # с какого баланса можно вывести DZO (кнопка «ВЫВОД ДЗО» видна всегда)
TOP_LIMIT = 200              # сколько участников показывает «!топ»
TOP_CUSTOM_EMOJI_ROWS = 25   # в стольких первых строках топа золотая монета (кастомный эмодзи)
# Telegram учитывает не больше 100 сущностей разметки на сообщение (ссылка,
# жирный, кастомный эмодзи — каждая отдельно). Топ режется на сообщения с запасом.
TOP_ENTITY_BUDGET = 90
WITHDRAW_RETRY_EVERY = 5 * 60        # как часто досылать недоставленные заявки на вывод
WITHDRAW_UNDELIVERED_TTL = 24 * 3600  # через сколько недоставленная заявка отклоняется с возвратом
# dzo_withdrawals.delivered: 0 не доставлена, 1 доставлена владельцу,
# 2 неизвестно (строки, созданные до появления колонки; по сроку не отклоняются).
DELIVERY_NO, DELIVERY_YES, DELIVERY_UNKNOWN = 0, 1, 2
UNDELIVERED_ALERT = (
    "Заявка создана, сумма заморожена, но владельцу она пока не дошла. "
    "Бот повторит отправку сам, можно и нажать кнопку ещё раз."
)
INVITE_BONUS = 50            # бонус за приглашённого друга
TRANSFER_FEE = 0.20          # комиссия за перевод — 20%
MIN_TRANSFER = 10            # минимальная сумма перевода
MUTE_DURATION = 10 * 60      # мут на 10 минут (покупной — суммируется)
PENDING_TTL = 15 * 60        # сколько ждём данные после покупки авто-товара
FISH_USES = 10               # на сколько рыбалок хватает удочки и червей
FISH_COOLDOWN = 2 * 3600     # после поломки удочки новую можно купить через 2 часа
BUSINESS_DURATION = 7 * 86400   # бизнес покупается на неделю
AUCTION_DURATION = 3600         # аукцион длится ровно час
DEFAULT_MIN_BID = 1000          # первоначальный взнос на аукционе

# Кому разрешена команда «!сбросить мои приглашения» (временно, для теста).
RESET_INVITE_ALLOWED = {6818495373, 435582277}

# ---- Магазин товаров -------------------------------------------------------
# kind: pin / mute / tag — автоматические; manual — уведомляем владельца.
SHOP_ITEMS = {
    "pin_1h":  {"name": "Закреп на час",        "price": 200,   "kind": "pin", "secs": 3600,
                "emoji": "📌", "desc": "Бот запросит текст/медиа и закрепит его на 1 час."},
    "pin_12h": {"name": "Закреп на 12 часов",   "price": 450,   "kind": "pin", "secs": 12 * 3600,
                "emoji": "📌", "desc": "Бот запросит текст/медиа и закрепит его на 12 часов."},
    "pin":     {"name": "Закреп на 1 сутки",    "price": 700,   "kind": "pin", "secs": 24 * 3600,
                "emoji": "📌", "desc": "Бот запросит текст/медиа и закрепит его на 24 часа."},
    "mute":    {"name": "Мут на 10 минут",      "price": 100,   "kind": "mute",
                "emoji": "🔇", "desc": "Мут на 10 минут. Повторная покупка продлевает мут."},
    "tag":     {"name": "Тег",                  "price": 300,   "kind": "tag",
                "emoji": "🏷", "desc": "Личный титул, который виден рядом с вашим именем."},
    "dzo":     {"name": "100 DZO",              "price": 1000,  "kind": "manual",
                "emoji": DZO_EMOJI, "desc": "Внутренняя валюта/ресурс — выдаёт владелец."},
    "mod":     {"name": "Модератор",            "price": 10000, "kind": "manual",
                "emoji": "🛡", "desc": "Выдача прав модератора — решает владелец."},
    "ad":      {"name": "Реклама клана (сутки)", "price": 3000, "kind": "manual",
                "emoji": "📢", "desc": "Размещение рекламы — согласует владелец."},
}
# Порядок важен: более конкретные ключи проверяются раньше («закреп на час» до «закреп»).
ITEM_ALIASES = {
    "pin_1h":  ("закреп на час", "закреп час"),
    "pin_12h": ("закреп на 12", "закреп 12"),
    "pin":     ("закреп",),
    "mute":    ("мут",),
    "tag":     ("тег",),
    "dzo":     ("dzo", "дзо"),
    "mod":     ("модератор",),
    "ad":      ("реклама",),
}

# ---- Рыболовный магазин ----------------------------------------------------
GEAR = {
    "rod":   {"name": "Удочка", "emoji": "🎣", "price": 150,
              "desc": f"Хватает на {FISH_USES} рыбалок. Может сломаться о старый сапог."},
    "worms": {"name": "Черви",  "emoji": "🪱", "price": 50,
              "desc": f"Наживка на {FISH_USES} рыбалок."},
}
GEAR_ALIASES = {"rod": ("удочк",), "worms": ("черв",)}

# Улов: weight — шанс выпадения, price — за сколько участник продаёт.
FISH = {
    "moyva": {"name": "Мойва",         "emoji": "🐟", "price": 10,  "weight": 300},
    "okun":  {"name": "Окунь",         "emoji": "🐟", "price": 20,  "weight": 250},
    "shuka": {"name": "Щука",          "emoji": "🐟", "price": 40,  "weight": 150},
    "sudak": {"name": "Судак",         "emoji": "🐟", "price": 60,  "weight": 100},
    "losos": {"name": "Лосось",        "emoji": "🐟", "price": 100, "weight": 70},
    "osetr": {"name": "Осётр",         "emoji": "🐟", "price": 200, "weight": 30},
    "gold":  {"name": "Золотая рыбка", "emoji": "🐠", "price": 500, "weight": 10},
    "boot":  {"name": "Старый сапог",  "emoji": "🥾", "price": 0,   "weight": 90},
}
FISH_ALIASES = {
    "moyva": ("мойв",), "okun": ("окун",), "shuka": ("щук",), "sudak": ("судак",),
    "losos": ("лосос", "лосось"), "osetr": ("осетр", "осётр"),
    "gold": ("золот",), "boot": ("сапог", "сапож"),
}

# ---- Бизнесы ---------------------------------------------------------------
# share — доля владельца бизнеса с каждой покупки в его магазине.
BUSINESSES = {
    "fishshop": {"name": "Рыболовный магазин", "emoji": "🎣", "price": 1000, "share": 0.05},
    "shop":     {"name": "Магазин товаров",    "emoji": "🏪", "price": 1000, "share": 0.02},
}
BUSINESS_ALIASES = {
    "fishshop": ("рыболовн", "рыбол"),
    "shop":     ("магазин товаров", "магазин"),
}

# Цены по умолчанию для всех продаваемых сущностей (используется get_price).
DEFAULT_PRICES = {}
for _k, _v in SHOP_ITEMS.items():
    DEFAULT_PRICES[_k] = _v["price"]
for _k, _v in GEAR.items():
    DEFAULT_PRICES[_k] = _v["price"]
for _k, _v in FISH.items():
    DEFAULT_PRICES[_k] = _v["price"]
for _k, _v in BUSINESSES.items():
    DEFAULT_PRICES[f"biz_{_k}"] = _v["price"]

# Ожидание данных после покупки авто-товара: (chat_id, user_id) -> состояние.
PENDING: dict[tuple[int, int], dict] = {}

# Кэш уже записанных в БД пользователей, чтобы не писать в БД на каждое сообщение.
# (chat_id, user_id) -> (full_name, username)
_SEEN_USERS: dict[tuple[int, int], tuple] = {}

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
        "photo": None,          # file_id картинки (если задана — шлём с ней)
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
        # Балансы DZO + время последнего фарма + кэш имени для «!топ».
        conn.execute(
            """CREATE TABLE IF NOT EXISTS cp_balances (
                   chat_id   INTEGER,
                   user_id   INTEGER,
                   balance   INTEGER DEFAULT 0,
                   last_farm INTEGER DEFAULT 0,
                   name      TEXT,
                   username  TEXT,
                   PRIMARY KEY (chat_id, user_id)
               )"""
        )
        # Кто уже принёс бонус за приглашение (защита от повторной выдачи).
        conn.execute(
            """CREATE TABLE IF NOT EXISTS cp_invited (
                   chat_id INTEGER,
                   user_id INTEGER,
                   PRIMARY KEY (chat_id, user_id)
               )"""
        )
        # Индивидуальные цены товаров по чатам (переопределяют дефолтные).
        conn.execute(
            """CREATE TABLE IF NOT EXISTS cp_prices (
                   chat_id INTEGER,
                   item    TEXT,
                   price   INTEGER,
                   PRIMARY KEY (chat_id, item)
               )"""
        )
        # Персональные ссылки-приглашения: по какой ссылке зашёл — тот и пригласил.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS cp_invite_links (
                   chat_id INTEGER,
                   link    TEXT,
                   user_id INTEGER,
                   PRIMARY KEY (chat_id, link)
               )"""
        )
        # Числовые настройки чата (напр. размер реферального бонуса, мин. ставка).
        conn.execute(
            """CREATE TABLE IF NOT EXISTS cp_settings (
                   chat_id INTEGER,
                   key     TEXT,
                   value   INTEGER,
                   PRIMARY KEY (chat_id, key)
               )"""
        )
        # Инвентарь: рыба + снасти (rod/worms хранят остаток рыбалок).
        conn.execute(
            """CREATE TABLE IF NOT EXISTS cp_inventory (
                   chat_id INTEGER,
                   user_id INTEGER,
                   item    TEXT,
                   qty     INTEGER DEFAULT 0,
                   PRIMARY KEY (chat_id, user_id, item)
               )"""
        )
        # Личные титулы (услуга «Тег»).
        conn.execute(
            """CREATE TABLE IF NOT EXISTS cp_titles (
                   chat_id INTEGER,
                   user_id INTEGER,
                   title   TEXT,
                   PRIMARY KEY (chat_id, user_id)
               )"""
        )
        # Владение бизнесами (на неделю).
        conn.execute(
            """CREATE TABLE IF NOT EXISTS cp_business (
                   chat_id    INTEGER,
                   business   TEXT,
                   owner_id   INTEGER,
                   expires_ts INTEGER,
                   PRIMARY KEY (chat_id, business)
               )"""
        )
        # Активные аукционы (переживают перезапуск бота).
        conn.execute(
            """CREATE TABLE IF NOT EXISTS cp_auction (
                   chat_id    INTEGER,
                   business   TEXT,
                   ends_ts    INTEGER,
                   min_bid    INTEGER,
                   top_bid    INTEGER DEFAULT 0,
                   top_bidder INTEGER,
                   PRIMARY KEY (chat_id, business)
               )"""
        )
        # Заявки на вывод DZO. Сумма списывается с баланса при подаче заявки
        # («заморозка»), при отклонении возвращается.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS dzo_withdrawals (
                   id          INTEGER PRIMARY KEY AUTOINCREMENT,
                   chat_id     INTEGER NOT NULL,
                   user_id     INTEGER NOT NULL,
                   username    TEXT,
                   name        TEXT,
                   amount      INTEGER NOT NULL,
                   status      TEXT NOT NULL DEFAULT 'pending',
                   created_ts  INTEGER NOT NULL,
                   decided_ts  INTEGER,
                   decided_by  INTEGER,
                   delivered   INTEGER NOT NULL DEFAULT 0
               )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_dzo_withdrawals_user "
            "ON dzo_withdrawals (chat_id, user_id, status)"
        )
        conn.commit()
    migrate_withdrawals_delivered()


def migrate_withdrawals_delivered() -> None:
    """ Таблица заявок могла появиться раньше без колонки delivered. Для таких
        строк неизвестно, видел ли заявку владелец, поэтому им ставится
        DELIVERY_UNKNOWN: фоновая задача их не досылает и НЕ отклоняет по сроку,
        решение остаётся за владельцем (или участник сам повторно жмёт кнопку).
        Колонка и разметка старых строк добавляются в одной транзакции. """
    with closing(sqlite3.connect(DB_PATH, isolation_level=None)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(dzo_withdrawals)")}
            if "delivered" in cols:
                conn.execute("ROLLBACK")
                return
            conn.execute(
                "ALTER TABLE dzo_withdrawals ADD COLUMN delivered INTEGER NOT NULL DEFAULT 0"
            )
            conn.execute("UPDATE dzo_withdrawals SET delivered=?", (DELIVERY_UNKNOWN,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


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
# Экономика DZO: работа с балансами, ценами и приглашениями
# ----------------------------------------------------------------------------
def now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def remember_user(chat_id: int, user) -> None:
    """ Запоминаем имя/юзернейм пользователя (нужно для «!топ»). """
    if not user:
        return
    name = getattr(user, "full_name", None)
    username = getattr(user, "username", None)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT INTO cp_balances (chat_id, user_id, name, username) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET "
            "name=COALESCE(excluded.name, cp_balances.name), "
            "username=COALESCE(excluded.username, cp_balances.username)",
            (chat_id, user.id, name, username),
        )
        conn.commit()


def get_display_name(chat_id: int, user_id: int) -> str:
    """ Имя пользователя из кэша БД (для показа, когда объекта юзера нет). """
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT name, username FROM cp_balances WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()
    if not row:
        return f"id{user_id}"
    return row[0] or (f"@{row[1]}" if row[1] else f"id{user_id}")


def find_user_by_username(chat_id: int, username: str):
    """ Ищет id пользователя по @нику среди тех, кого бот уже видел в чате. """
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT user_id, name, username FROM cp_balances "
            "WHERE chat_id=? AND lower(username)=lower(?) LIMIT 1",
            (chat_id, username.lstrip("@")),
        ).fetchone()
    return row  # (user_id, name, username) либо None


def get_balance(chat_id: int, user_id: int) -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT balance FROM cp_balances WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()
        return row[0] if row else 0


def add_balance(chat_id: int, user_id: int, amount: int, user=None) -> int:
    """ Меняем баланс на amount (может быть отрицательным). Возвращает новый баланс. """
    name = getattr(user, "full_name", None) if user else None
    username = getattr(user, "username", None) if user else None
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT INTO cp_balances (chat_id, user_id, balance, name, username) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET "
            "balance=cp_balances.balance+excluded.balance, "
            "name=COALESCE(excluded.name, cp_balances.name), "
            "username=COALESCE(excluded.username, cp_balances.username)",
            (chat_id, user_id, amount, name, username),
        )
        conn.commit()
        row = conn.execute(
            "SELECT balance FROM cp_balances WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()
        return row[0] if row else 0


def get_farm_time(chat_id: int, user_id: int) -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT last_farm FROM cp_balances WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()
        return row[0] if row and row[0] else 0


def set_farm_time(chat_id: int, user_id: int, ts: int) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT INTO cp_balances (chat_id, user_id, last_farm) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET last_farm=excluded.last_farm",
            (chat_id, user_id, ts),
        )
        conn.commit()


def get_top(chat_id: int, limit=None):
    """ Все участники с ненулевым балансом. limit=None — без ограничения. """
    sql = ("SELECT user_id, balance, name, username FROM cp_balances "
           "WHERE chat_id=? AND balance>0 ORDER BY balance DESC")
    params = [chat_id]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        return conn.execute(sql, params).fetchall()


def was_invited(chat_id: int, user_id: int) -> bool:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT 1 FROM cp_invited WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()
        return row is not None


def mark_invited(chat_id: int, user_id: int) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO cp_invited (chat_id, user_id) VALUES (?, ?)",
            (chat_id, user_id),
        )
        conn.commit()


def reset_invited(chat_id: int, user_id: int) -> None:
    """ Снимает пометку «уже приносил бонус» с одного пользователя. """
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "DELETE FROM cp_invited WHERE chat_id=? AND user_id=?", (chat_id, user_id)
        )
        conn.commit()


def get_invite_bonus(chat_id: int) -> int:
    """ Размер реферального бонуса чата (по умолчанию INVITE_BONUS). """
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT value FROM cp_settings WHERE chat_id=? AND key='invite_bonus'",
            (chat_id,),
        ).fetchone()
    return row[0] if row else INVITE_BONUS


def set_invite_bonus(chat_id: int, value: int) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT INTO cp_settings (chat_id, key, value) VALUES (?, 'invite_bonus', ?) "
            "ON CONFLICT(chat_id, key) DO UPDATE SET value=excluded.value",
            (chat_id, value),
        )
        conn.commit()


def take_balance(chat_id: int, user_id: int, amount, user=None):
    """ Списывает amount DZO (команда «!забрать»), баланс не уходит ниже нуля.
        amount=None — забрать всё. Одной транзакцией. Возвращает (сколько
        реально списано, новый баланс). Замороженные в заявках на вывод суммы
        не затрагиваются: они уже не на балансе. """
    name = getattr(user, "full_name", None) if user else None
    username = getattr(user, "username", None) if user else None
    with closing(sqlite3.connect(DB_PATH, isolation_level=None)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT balance FROM cp_balances WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            ).fetchone()
            bal = row[0] if row and row[0] else 0
            if amount is None:          # всё, как «!обнулить»
                taken, new_bal = max(0, bal), 0
            else:
                taken = max(0, min(int(amount), bal))
                new_bal = bal - taken
            if row:
                conn.execute(
                    "UPDATE cp_balances SET balance=?, name=COALESCE(?, name), "
                    "username=COALESCE(?, username) WHERE chat_id=? AND user_id=?",
                    (new_bal, name, username, chat_id, user_id),
                )
            conn.execute("COMMIT")
            return taken, new_bal
        except Exception:
            conn.execute("ROLLBACK")
            raise


def set_balance(chat_id: int, user_id: int, amount: int, user=None) -> None:
    """ Жёстко выставить баланс (используется командой «!обнулить»). """
    name = getattr(user, "full_name", None) if user else None
    username = getattr(user, "username", None) if user else None
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT INTO cp_balances (chat_id, user_id, balance, name, username) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET balance=excluded.balance, "
            "name=COALESCE(excluded.name, cp_balances.name), "
            "username=COALESCE(excluded.username, cp_balances.username)",
            (chat_id, user_id, amount, name, username),
        )
        conn.commit()


def get_price(chat_id: int, item: str) -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT price FROM cp_prices WHERE chat_id=? AND item=?",
            (chat_id, item),
        ).fetchone()
    return row[0] if row else DEFAULT_PRICES.get(item, 0)


def set_price(chat_id: int, item: str, price: int) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT INTO cp_prices (chat_id, item, price) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id, item) DO UPDATE SET price=excluded.price",
            (chat_id, item, price),
        )
        conn.commit()


def get_user_link(chat_id: int, user_id: int):
    """ Ранее выданная персональная ссылка пользователя (чтобы не плодить новые). """
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT link FROM cp_invite_links WHERE chat_id=? AND user_id=? LIMIT 1",
            (chat_id, user_id),
        ).fetchone()
        return row[0] if row else None


def save_invite_link(chat_id: int, link: str, user_id: int) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO cp_invite_links (chat_id, link, user_id) VALUES (?, ?, ?)",
            (chat_id, link, user_id),
        )
        conn.commit()


def get_link_owner(chat_id: int, link: str):
    """ Кому принадлежит ссылка-приглашение (кто получит бонус). """
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT user_id FROM cp_invite_links WHERE chat_id=? AND link=?",
            (chat_id, link),
        ).fetchone()
        return row[0] if row else None


def get_setting(chat_id: int, key: str, default: int) -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT value FROM cp_settings WHERE chat_id=? AND key=?", (chat_id, key)
        ).fetchone()
    return row[0] if row else default


def set_setting(chat_id: int, key: str, value: int) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT INTO cp_settings (chat_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id, key) DO UPDATE SET value=excluded.value",
            (chat_id, key, value),
        )
        conn.commit()


# ---- Инвентарь (рыба + снасти) --------------------------------------------
def get_item_qty(chat_id: int, user_id: int, item: str) -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT qty FROM cp_inventory WHERE chat_id=? AND user_id=? AND item=?",
            (chat_id, user_id, item),
        ).fetchone()
        return row[0] if row else 0


def add_item(chat_id: int, user_id: int, item: str, qty: int) -> int:
    """ Меняет количество предмета на qty (может быть отрицательным). """
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT INTO cp_inventory (chat_id, user_id, item, qty) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(chat_id, user_id, item) DO UPDATE SET "
            "qty=MAX(0, cp_inventory.qty + excluded.qty)",
            (chat_id, user_id, item, qty),
        )
        conn.commit()
        row = conn.execute(
            "SELECT qty FROM cp_inventory WHERE chat_id=? AND user_id=? AND item=?",
            (chat_id, user_id, item),
        ).fetchone()
        return row[0] if row else 0


def set_item(chat_id: int, user_id: int, item: str, qty: int) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT INTO cp_inventory (chat_id, user_id, item, qty) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(chat_id, user_id, item) DO UPDATE SET qty=excluded.qty",
            (chat_id, user_id, item, max(0, qty)),
        )
        conn.commit()


def get_inventory(chat_id: int, user_id: int) -> dict:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        rows = conn.execute(
            "SELECT item, qty FROM cp_inventory WHERE chat_id=? AND user_id=? AND qty>0",
            (chat_id, user_id),
        ).fetchall()
    return {item: qty for item, qty in rows}


# ---- Титулы (услуга «Тег») -------------------------------------------------
def get_title(chat_id: int, user_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT title FROM cp_titles WHERE chat_id=? AND user_id=?", (chat_id, user_id)
        ).fetchone()
        return row[0] if row else None


def get_titles(chat_id: int) -> dict:
    """ Все титулы чата разом (чтобы не дёргать БД на каждого в топе). """
    with closing(sqlite3.connect(DB_PATH)) as conn:
        rows = conn.execute(
            "SELECT user_id, title FROM cp_titles WHERE chat_id=?", (chat_id,)
        ).fetchall()
    return {uid: title for uid, title in rows}


def set_title(chat_id: int, user_id: int, title: str) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT INTO cp_titles (chat_id, user_id, title) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET title=excluded.title",
            (chat_id, user_id, title),
        )
        conn.commit()


# ---- Бизнесы ---------------------------------------------------------------
def get_business(chat_id: int, business: str):
    """ (owner_id, expires_ts) действующего владельца либо None. """
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT owner_id, expires_ts FROM cp_business WHERE chat_id=? AND business=?",
            (chat_id, business),
        ).fetchone()
    if not row or row[1] <= now_ts():
        return None  # свободен или срок истёк
    return row


def set_business(chat_id: int, business: str, owner_id: int, expires_ts: int) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT INTO cp_business (chat_id, business, owner_id, expires_ts) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(chat_id, business) DO UPDATE SET "
            "owner_id=excluded.owner_id, expires_ts=excluded.expires_ts",
            (chat_id, business, owner_id, expires_ts),
        )
        conn.commit()


def user_business(chat_id: int, user_id: int):
    """ Какой бизнес уже принадлежит участнику (1 участник = 1 бизнес). """
    for key in BUSINESSES:
        row = get_business(chat_id, key)
        if row and row[0] == user_id:
            return key
    return None


# ---- Аукционы --------------------------------------------------------------
def get_auction(chat_id: int, business: str):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        return conn.execute(
            "SELECT ends_ts, min_bid, top_bid, top_bidder FROM cp_auction "
            "WHERE chat_id=? AND business=?",
            (chat_id, business),
        ).fetchone()


def all_auctions():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        return conn.execute(
            "SELECT chat_id, business, ends_ts FROM cp_auction"
        ).fetchall()


def start_auction(chat_id: int, business: str, ends_ts: int, min_bid: int) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT INTO cp_auction (chat_id, business, ends_ts, min_bid, top_bid, top_bidder) "
            "VALUES (?, ?, ?, ?, 0, NULL) ON CONFLICT(chat_id, business) DO UPDATE SET "
            "ends_ts=excluded.ends_ts, min_bid=excluded.min_bid, top_bid=0, top_bidder=NULL",
            (chat_id, business, ends_ts, min_bid),
        )
        conn.commit()


def set_top_bid(chat_id: int, business: str, bid: int, bidder: int) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "UPDATE cp_auction SET top_bid=?, top_bidder=? WHERE chat_id=? AND business=?",
            (bid, bidder, chat_id, business),
        )
        conn.commit()


def clear_auction(chat_id: int, business: str) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "DELETE FROM cp_auction WHERE chat_id=? AND business=?", (chat_id, business)
        )
        conn.commit()


# ---- Вывод DZO -------------------------------------------------------------
def get_pending_withdrawal(chat_id: int, user_id: int):
    """ Незакрытая заявка участника в этом чате: (id, amount) либо None. """
    with closing(sqlite3.connect(DB_PATH)) as conn:
        return conn.execute(
            "SELECT id, amount FROM dzo_withdrawals "
            "WHERE chat_id=? AND user_id=? AND status='pending' ORDER BY id LIMIT 1",
            (chat_id, user_id),
        ).fetchone()


def create_withdrawal(chat_id: int, user, min_amount: int):
    """ Замораживает ВЕСЬ баланс участника в новой заявке на вывод.
        Всё в одной транзакции: баланс обнуляется, заявка создаётся.
        Возвращает ("ok", id, amount), ("pending", id, amount) если заявка уже есть,
        или ("low", None, balance) если баланс меньше min_amount. """
    with closing(sqlite3.connect(DB_PATH, isolation_level=None)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT id, amount FROM dzo_withdrawals "
                "WHERE chat_id=? AND user_id=? AND status='pending' ORDER BY id LIMIT 1",
                (chat_id, user.id),
            ).fetchone()
            if row:
                conn.execute("ROLLBACK")
                return "pending", row[0], row[1]
            brow = conn.execute(
                "SELECT balance FROM cp_balances WHERE chat_id=? AND user_id=?",
                (chat_id, user.id),
            ).fetchone()
            bal = brow[0] if brow and brow[0] else 0
            if bal < min_amount:
                conn.execute("ROLLBACK")
                return "low", None, bal
            conn.execute(
                "UPDATE cp_balances SET balance=balance-? WHERE chat_id=? AND user_id=?",
                (bal, chat_id, user.id),
            )
            cur = conn.execute(
                "INSERT INTO dzo_withdrawals "
                "(chat_id, user_id, username, name, amount, status, created_ts) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (chat_id, user.id, getattr(user, "username", None),
                 getattr(user, "full_name", None), bal, now_ts()),
            )
            conn.execute("COMMIT")
            return "ok", cur.lastrowid, bal
        except Exception:
            conn.execute("ROLLBACK")
            raise


def get_withdrawal(wid: int):
    """ (id, chat_id, user_id, username, name, amount, status) либо None. """
    with closing(sqlite3.connect(DB_PATH)) as conn:
        return conn.execute(
            "SELECT id, chat_id, user_id, username, name, amount, status "
            "FROM dzo_withdrawals WHERE id=?",
            (wid,),
        ).fetchone()


def mark_withdrawal_delivered(wid: int) -> None:
    """ Отметка «заявка у владельца». Меняет только delivered и только у
        открытой заявки; статус и балансы не трогает. """
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "UPDATE dzo_withdrawals SET delivered=? WHERE id=? AND status='pending'",
            (DELIVERY_YES, wid),
        )
        conn.commit()


def is_withdrawal_delivered(wid: int) -> bool:
    """ True только если доставка точно была (DELIVERY_YES). Для «неизвестно»
        False: повторное нажатие участника дошлёт заявку ещё раз (дубль безопасен). """
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute("SELECT delivered FROM dzo_withdrawals WHERE id=?", (wid,)).fetchone()
    return bool(row and row[0] == DELIVERY_YES)


def undelivered_withdrawals():
    """ Заявки pending, которые точно ещё не дошли до владельца (delivered=0):
        [(id, chat_id, user_id, amount, created_ts), ...]. Строки с неизвестным
        фактом доставки (DELIVERY_UNKNOWN) сюда не попадают. """
    with closing(sqlite3.connect(DB_PATH)) as conn:
        return conn.execute(
            "SELECT id, chat_id, user_id, amount, created_ts FROM dzo_withdrawals "
            "WHERE status='pending' AND delivered=? ORDER BY id",
            (DELIVERY_NO,),
        ).fetchall()


def is_withdrawal_undelivered_now(wid: int) -> bool:
    """ Актуальная перепроверка перед досылкой: заявка всё ещё открыта и не доставлена. """
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT 1 FROM dzo_withdrawals WHERE id=? AND status='pending' AND delivered=?",
            (wid, DELIVERY_NO),
        ).fetchone()
    return row is not None


def expire_withdrawal(wid: int, created_before: int) -> bool:
    """ Отклоняет по сроку недоставленную заявку и возвращает сумму. Всё условие
        (открыта, точно не доставлена, создана раньше created_before) проверяется
        в той же транзакции, что и возврат, поэтому заявку, которую успели
        доставить или по которой владелец уже решил, функция не трогает.
        Возвращает True, только если возврат действительно сделан. """
    with closing(sqlite3.connect(DB_PATH, isolation_level=None)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "UPDATE dzo_withdrawals SET status='rejected', decided_ts=?, decided_by=0 "
                "WHERE id=? AND status='pending' AND delivered=? AND created_ts<?",
                (now_ts(), wid, DELIVERY_NO, created_before),
            )
            if cur.rowcount != 1:
                conn.execute("ROLLBACK")
                return False
            chat_id, user_id, amount = conn.execute(
                "SELECT chat_id, user_id, amount FROM dzo_withdrawals WHERE id=?", (wid,)
            ).fetchone()
            conn.execute(
                "INSERT INTO cp_balances (chat_id, user_id, balance) VALUES (?, ?, ?) "
                "ON CONFLICT(chat_id, user_id) DO UPDATE SET "
                "balance=cp_balances.balance+excluded.balance",
                (chat_id, user_id, amount),
            )
            conn.execute("COMMIT")
            return True
        except Exception:
            conn.execute("ROLLBACK")
            raise


def decide_withdrawal(wid: int, approve: bool, decided_by: int) -> bool:
    """ Закрывает заявку. Срабатывает только один раз (status='pending' → итог),
        повторный вызов возвращает False и ничего не меняет.
        При отклонении замороженная сумма возвращается на баланс в той же транзакции. """
    with closing(sqlite3.connect(DB_PATH, isolation_level=None)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT chat_id, user_id, amount FROM dzo_withdrawals "
                "WHERE id=? AND status='pending'",
                (wid,),
            ).fetchone()
            if not row:
                conn.execute("ROLLBACK")
                return False
            chat_id, user_id, amount = row
            conn.execute(
                "UPDATE dzo_withdrawals SET status=?, decided_ts=?, decided_by=? "
                "WHERE id=? AND status='pending'",
                ("approved" if approve else "rejected", now_ts(), decided_by, wid),
            )
            if not approve:
                conn.execute(
                    "INSERT INTO cp_balances (chat_id, user_id, balance) VALUES (?, ?, ?) "
                    "ON CONFLICT(chat_id, user_id) DO UPDATE SET "
                    "balance=cp_balances.balance+excluded.balance",
                    (chat_id, user_id, amount),
                )
            conn.execute("COMMIT")
            return True
        except Exception:
            conn.execute("ROLLBACK")
            raise


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
    except TelegramAPIError:
        return False
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)


async def owner_check(bot: Bot, chat_id: int, user_id: int):
    """ True/False: создатель ли user_id в чате; None, если Telegram не ответил
        (например, бота удалили из группы). """
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except TelegramAPIError as e:
        log.warning("Не удалось проверить владельца чата %s: %s", chat_id, e)
        return None
    return member.status == ChatMemberStatus.CREATOR


async def is_owner(bot: Bot, chat_id: int, user_id: int) -> bool:
    """ True только для создателя (владельца) чата. """
    return bool(await owner_check(bot, chat_id, user_id))


# ---- Запасной путь для кастомного эмодзи ------------------------------------
_TG_EMOJI_RE = re.compile(r'<tg-emoji emoji-id="(\d+)">(.*?)</tg-emoji>', re.S)
_CUSTOM_EMOJI_REJECTED = False   # Telegram уже отклонял кастомный эмодзи этого бота


def plain_emoji_text(text):
    """ Тот же текст, но вместо <tg-emoji ...>🪙</tg-emoji> жёлтый 🟡
        (обычная 🪙 на телефонах серая). """
    if not text:
        return text
    # Чужие кастомные эмодзи (например, в тексте рекламы от админа) заменяются
    # своим замещающим символом, монета DZO заменяется на 🟡.
    return _TG_EMOJI_RE.sub(
        lambda m: DZO_EMOJI_PLAIN if m.group(1) == DZO_EMOJI_ID else m.group(2), text)


_BUTTON_ICON_REJECTED = False   # Telegram уже отклонял иконку на кнопке


def _markup_has_icons(markup) -> bool:
    return isinstance(markup, InlineKeyboardMarkup) and any(
        getattr(b, "icon_custom_emoji_id", None) for row in markup.inline_keyboard for b in row
    )


def _plain_coin_button(b):
    """ Кнопка без иконки: монета жёлтым 🟡 в начале текста. """
    if not getattr(b, "icon_custom_emoji_id", None):
        return b
    return b.model_copy(update={"icon_custom_emoji_id": None,
                                "text": f"{DZO_EMOJI_PLAIN} {b.text}"})


def strip_button_icons(markup):
    """ Та же клавиатура, но без icon_custom_emoji_id на кнопках
        (вместо иконки в текст кнопки ставится 🟡). """
    if not _markup_has_icons(markup):
        return markup
    return InlineKeyboardMarkup(inline_keyboard=[
        [_plain_coin_button(b) for b in row]
        for row in markup.inline_keyboard
    ])


async def emoji_fallback_middleware(make_request, bot, method):
    """ Middleware сессии бота: запасной путь для кастомных эмодзи.
        1) <tg-emoji> в полях text (sendMessage, editMessageText) и caption
           (sendPhoto, editMessageCaption и др.), включая рекламу;
        2) иконка icon_custom_emoji_id на inline-кнопках с ценой.
        Если Telegram отклонил запрос (TelegramBadRequest), он повторяется
        сначала без иконок на кнопках, потом с жёлтым 🟡 в тексте, потом без
        того и другого. Что именно пришлось убрать, запоминается до перезапуска,
        и дальше это убирается сразу, без лишних запросов. Повторяется только
        отправка: начисления, кулдауны и заморозки уже записаны и второй раз
        не выполняются. """
    global _CUSTOM_EMOJI_REJECTED, _BUTTON_ICON_REJECTED
    text_update = {}
    for field in _EMOJI_FIELDS:
        value = getattr(method, field, None)
        if isinstance(value, str) and "<tg-emoji" in value:
            text_update[field] = plain_emoji_text(value)
    markup = getattr(method, "reply_markup", None)
    has_icons = _markup_has_icons(markup)
    if not text_update and not has_icons:
        return await make_request(bot, method)

    def variant(strip_text: bool, strip_icons: bool):
        update = {}
        if strip_text and text_update:
            update.update(text_update)
        if strip_icons and has_icons:
            update["reply_markup"] = strip_button_icons(markup)
        return method.model_copy(update=update) if update else method

    force_text = _CUSTOM_EMOJI_REJECTED and bool(text_update)
    force_icons = _BUTTON_ICON_REJECTED and has_icons
    # Порядок попыток: как есть → без иконок → без tg-emoji → без обоих.
    attempts = []
    for st, si in ((False, False), (False, True), (True, False), (True, True)):
        st, si = st or force_text, si or force_icons
        if (st and not text_update) or (si and not has_icons):
            continue
        if (st, si) not in attempts:
            attempts.append((st, si))
    last_error = None
    for st, si in attempts:
        try:
            result = await make_request(bot, variant(st, si))
        except TelegramBadRequest as e:
            last_error = e
            continue
        if last_error is not None:
            if st and not _CUSTOM_EMOJI_REJECTED:
                _CUSTOM_EMOJI_REJECTED = True
                log.warning("Telegram отклонил кастомный эмодзи (%s), дальше обычный %s",
                            last_error, DZO_EMOJI_PLAIN)
            if si and not _BUTTON_ICON_REJECTED:
                _BUTTON_ICON_REJECTED = True
                log.warning("Telegram отклонил иконку на кнопке (%s), дальше кнопки без неё",
                            last_error)
        return result
    raise last_error


_EMOJI_FIELDS = ("text", "caption")


def esc(text) -> str:
    return (str(text) if text is not None else "").replace("<", "&lt;").replace(">", "&gt;")


class SimpleUser:
    """ Лёгкая замена объекта пользователя, когда есть только данные из БД
        (нужно, чтобы @ник резолвился без обращения к Telegram). """
    def __init__(self, user_id: int, full_name=None, username=None):
        self.id = user_id
        self.full_name = full_name
        self.username = username
        self.is_bot = False


def cp(amount) -> str:
    """ Единый формат суммы для HTML-сообщений: «100 <монета> DZO». """
    return f"{amount} {DZO_EMOJI} {CURRENCY}"


def cp_plain(amount) -> str:
    """ Формат суммы для всплывающих окон и мест без кастомного эмодзи
        (без HTML): «100 🟡 DZO». """
    return f"{amount} {DZO_EMOJI_PLAIN} {CURRENCY}"


# ---- Топ: строки и разбивка на сообщения -----------------------------------
_TOP_MEDALS = ("🥇", "🥈", "🥉")
# То, что Telegram сам превращает в сущности в обычном тексте (@ник, #тег, $тикер,
# /команда, ссылка). Встречается только в титулах; считаем с запасом.
_AUTO_ENTITY_RE = re.compile(
    r"[@#$/]\w|https?://|\w\.[^\W\d_]{2,}"
    r"|\+?\d[\d\-() ]{5,}\d"   # номер телефона / длинное число (Telegram может выделить)
)


def html_entity_count(html: str) -> int:
    """ Сколько сущностей разметки Telegram насчитает в HTML-строке бота:
        теги <a>, <b>, <i>, <code>, <tg-emoji> плюс автоссылки (@, #, $, /, домены)
        в обычном тексте. Считает с запасом, точность не нужна. """
    tags = len(re.findall(r"<(?:a|b|i|code|tg-emoji)[\s>]", html))
    # Текст внутри ссылки уже входит в её сущность, автоссылкой не считается.
    # Жирным у бота идут только суммы и заголовок, их не разбираем (иначе
    # баланс от 1 000 000 считался бы номером телефона).
    plain = re.sub(r"<a\s[^>]*>.*?</a>|<b>.*?</b>", " ", html, flags=re.S)
    plain = re.sub(r"<[^>]+>", " ", plain)
    return tags + len(_AUTO_ENTITY_RE.findall(plain))


def top_line(i: int, uid: int, bal: int, name=None, username=None, title=None) -> str:
    """ Строка «!топ». В первых TOP_CUSTOM_EMOJI_ROWS строках сумма с золотой
        монетой (кастомный эмодзи, как в магазине), дальше с жёлтым 🟡:
        кастомных эмодзи в одном сообщении Telegram учитывает ограниченно. """
    tag = _TOP_MEDALS[i] if i < len(_TOP_MEDALS) else f"{i + 1}."
    disp = esc(name or (f"@{username}" if username else None) or f"id{uid}")
    link = f'<a href="tg://user?id={uid}">{disp}</a>'
    if title:
        link += f" [{esc(title)}]"
    amount = cp(bal) if i < TOP_CUSTOM_EMOJI_ROWS else cp_plain(bal)
    return f"{tag} {link} — <b>{amount}</b>"


def top_header(shown: str) -> str:
    return f"🏆 <b>Топ по {DZO_EMOJI} DZO</b> ({shown})"


def top_chunks(header: str, lines, max_chars: int = 3800, max_entities: int = None):
    """ Режет топ на сообщения: не больше max_chars символов (лимит Telegram 4096)
        и не больше max_entities сущностей (лимит Telegram 100) в каждом. """
    if max_entities is None:
        max_entities = TOP_ENTITY_BUDGET
    chunks = []
    chunk, ents = header, html_entity_count(header)
    for line in lines:
        n = html_entity_count(line)
        if chunk and (len(chunk) + len(line) + 1 > max_chars or ents + n > max_entities):
            chunks.append(chunk)
            chunk, ents = "", 0
        chunk = f"{chunk}\n{line}" if chunk else line
        ents += n
    if chunk:
        chunks.append(chunk)
    return chunks


def match_key(text: str, aliases: dict):
    """ Ищет ключ каталога по алиасам в тексте (порядок словаря = приоритет). """
    low = (text or "").lower()
    for key, words in aliases.items():
        if any(w in low for w in words):
            return key
    return None


def titled(chat_id: int, user) -> str:
    """ Упоминание пользователя с личным титулом (услуга «Тег»), если он есть. """
    base = mention(user)
    title = get_title(chat_id, user.id)
    return f"{base} [{esc(title)}]" if title else base


def roll_fish() -> str:
    """ Случайный улов по весам из каталога FISH. """
    keys = list(FISH)
    weights = [FISH[k]["weight"] for k in keys]
    return random.choices(keys, weights=weights)[0]


def is_group(message: Message) -> bool:
    return message.chat.type in ("group", "supergroup")


def farm_amount() -> int:
    """ Случайные 1–3 DZO, у каждого значения равный шанс. """
    return random.randint(FARM_MIN, FARM_MAX)


def extract_amount(text: str):
    """ Число-сумма из текста. @упоминания и tg-ссылки вырезаем, чтобы цифры
        из ников не путались с суммой. Берём последнее число. """
    clean = re.sub(r"@[A-Za-z0-9_]+", " ", text or "")
    clean = re.sub(r"tg://\S+", " ", clean)
    nums = re.findall(r"\d+", clean)
    return int(nums[-1]) if nums else None


def _utf16_to_index(text: str, offset: int) -> int:
    """ Смещение Telegram (в UTF-16) → индекс символа в строке Python. """
    units = 0
    for i, ch in enumerate(text):
        if units >= offset:
            return i
        units += 2 if ord(ch) > 0xFFFF else 1
    return len(text)


TAKE_FORMAT = ("Формат: <code>!забрать @ник 500</code> (или ответом на сообщение "
               "<code>!забрать 500</code>). Без суммы забираются все DZO.")
_TAKE_WORD_RE = re.compile(r"(?i)^забрать(?=\s|$)")
_TG_USER_RE = re.compile(r"^tg://user\?id=(\d+)$")


async def _member_user(message: Message, user_id: int):
    """ Участник чата по ID; вышедших и исключённых не считаем участниками. """
    try:
        member = await message.bot.get_chat_member(message.chat.id, user_id)
    except TelegramAPIError:
        return None
    if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        return None
    if getattr(member, "is_member", True) is False:   # ограниченный, но уже вышел
        return None
    return member.user


def _real_reply(message: Message):
    """ Сообщение, на которое ответили, или None. В форум-группе Telegram кладёт
        в reply_to_message служебное сообщение о создании темы: это не ответ. """
    reply = message.reply_to_message
    if not reply:
        return None
    if getattr(reply, "forum_topic_created", None):
        return None
    if getattr(message, "is_topic_message", None) and \
            reply.message_id == getattr(message, "message_thread_id", None):
        return None
    return reply


async def parse_take(message: Message):
    """ Разбор «!забрать»: возвращает (цель, сумма, ошибка).
        Цель: ответ на сообщение, скрытое упоминание (text_mention), @ник,
        ссылка tg://user?id=, числовой ID. Сумма: одно целое число больше нуля
        или ничего (None — забрать всё). Всё остальное (лишние слова, «-500»,
        «10.5», «abc», неизвестный @ник) — ошибка, баланс не трогается. """
    text = message.text or ""
    m = _TAKE_WORD_RE.match(text)
    if not m:
        return None, None, TAKE_FORMAT
    body = text[m.end():]
    target = None
    # скрытое упоминание: вырезаем ровно его участок текста
    mentions = []
    for ent in message.entities or []:
        if ent.type == "text_mention" and ent.user:
            start = _utf16_to_index(text, ent.offset)
            end = _utf16_to_index(text, ent.offset + ent.length)
            if start >= m.end():
                mentions.append((start, end, ent.user))
    if len(mentions) > 1:
        return None, None, TAKE_FORMAT   # несколько целей сразу
    if mentions:
        start, end, target = mentions[0]
        body = text[m.end():start] + " " + text[end:]
    reply = _real_reply(message)
    if reply:
        if target is not None:
            return None, None, TAKE_FORMAT
        if reply.sender_chat or not reply.from_user or reply.from_user.is_bot:
            return None, None, ("Ответьте командой на сообщение самого участника "
                                "(не бота и не канала) или укажите @ник.")
        target = reply.from_user
    tokens = body.split()
    nums = []
    for tok in tokens:
        if tok.startswith("@"):
            if target is not None or not re.fullmatch(r"@[A-Za-z0-9_]{4,32}", tok):
                return None, None, TAKE_FORMAT
            uname = tok[1:]
            row = find_user_by_username(message.chat.id, uname)
            if row:
                # Ник в базе мог устареть (участник сменил его, ник занял другой):
                # перед списанием сверяем с Telegram, что ник сейчас у этого ID.
                target = await _member_user(message, row[0])
                if target is None or (target.username or "").lower() != uname.lower():
                    return None, None, (f"Не уверен, кто сейчас {esc(tok)}: ник мог смениться. "
                                        "Ответьте командой на сообщение участника.")
            else:
                try:
                    target = await message.bot.get_chat("@" + uname)
                except Exception:  # noqa: BLE001 — ник может не находиться
                    target = None
                # @ник канала или группы — не участник; человек должен быть в этом чате
                if target is not None and getattr(target, "type", None) == "private":
                    target = await _member_user(message, target.id)
                else:
                    target = None
                if target is None:
                    return None, None, f"Не нашёл {esc(tok)} в чате. Ответьте командой на его сообщение."
        elif _TG_USER_RE.match(tok):
            if target is not None:
                return None, None, TAKE_FORMAT
            target = await _member_user(message, int(_TG_USER_RE.match(tok).group(1)))
            if target is None:
                return None, None, "Не нашёл этого участника в чате."
        elif tok.isdigit() and tok.isascii():
            nums.append(int(tok))
        else:
            return None, None, TAKE_FORMAT
    if target is None:
        # без явной цели первое число — ID участника, второе (если есть) — сумма
        if not nums or len(nums) > 2 or nums[0] < 10_000:
            return None, None, TAKE_FORMAT
        target = await _member_user(message, nums.pop(0))
        if target is None:
            return None, None, "Не нашёл этого участника в чате."
    if len(nums) > 1:
        return None, None, TAKE_FORMAT
    amount = nums[0] if nums else None
    if amount == 0:
        return None, None, "Сумма должна быть больше нуля. " + TAKE_FORMAT
    return target, amount, None


async def resolve_target(message: Message, numeric: bool = False):
    """ Определяет пользователя-цель для перевода/выдачи:
        reply → text_mention → tg://user ссылка → @username → (опц.) числовой ID.
        Возвращает объект с .id/.full_name/.username (User или Chat) либо None. """
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user
    text = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or []
    for ent in entities:
        if ent.type == "text_mention" and ent.user:
            return ent.user
    m = re.search(r"tg://user\?id=(\d+)", text)
    if m:
        try:
            cm = await message.bot.get_chat_member(message.chat.id, int(m.group(1)))
            return cm.user
        except TelegramBadRequest:
            pass
    m = re.search(r"@([A-Za-z0-9_]{4,32})", text)
    if m:
        # 1) сначала ищем среди тех, кого бот уже видел в чате (надёжно, без Telegram)
        row = find_user_by_username(message.chat.id, m.group(1))
        if row:
            return SimpleUser(row[0], full_name=row[1], username=row[2])
        # 2) фолбэк: пробуем спросить Telegram (сработает не для всех)
        try:
            return await message.bot.get_chat("@" + m.group(1))
        except Exception:  # noqa: BLE001 — username может не резолвиться
            pass
    if numeric:
        m = re.search(r"\b(\d{5,})\b", text)
        if m:
            try:
                cm = await message.bot.get_chat_member(message.chat.id, int(m.group(1)))
                return cm.user
            except TelegramBadRequest:
                pass
    return None


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

    async def send_to(self, chat_id: int) -> None:
        """ Отправляет рекламу: с картинкой, если она задана, иначе текстом. """
        text = (self.settings.get("text") or "").strip()
        photo = self.settings.get("photo")
        if photo and text and len(text) <= 1024:
            await self.bot.send_photo(chat_id, photo, caption=text)
        elif photo:
            await self.bot.send_photo(chat_id, photo)
            if text:
                await self.bot.send_message(chat_id, text)
        elif text:
            await self.bot.send_message(chat_id, text)

    async def _loop(self):
        try:
            while True:
                interval = max(10, int(self.settings["interval_seconds"]))
                await asyncio.sleep(interval)
                chat_id = self.settings.get("chat_id")
                has_content = self.settings.get("text") or self.settings.get("photo")
                if has_content and chat_id:
                    try:
                        await self.send_to(chat_id)
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
CMD_PREFIX = "!"
_BANG_RE = re.compile(r"^\s*!\s*")


def split_bang(message: Message):
    """ Если сообщение начинается с «!» (пробелы и переносы перед ним допускаются,
        как у первого бота группы), возвращает его копию без «!» (и пробелов после
        него), со сдвинутыми сущностями (@упоминания, ссылки). Иначе None.
        Копия остаётся привязанной к боту, так что message.reply/answer работают. """
    text = message.text
    if not text or not text.lstrip().startswith(CMD_PREFIX):
        return None
    cut = _BANG_RE.match(text).end()
    rest = text[cut:]
    if not rest.strip():
        return None
    # «!» и пробельные символы — символы BMP, в UTF-16 это ровно cut единиц.
    new_entities = None
    if message.entities:
        new_entities = []
        for ent in message.entities:
            start = ent.offset - cut
            length = ent.length
            if start < 0:
                length += start
                start = 0
            if length > 0:
                new_entities.append(ent.model_copy(update={"offset": start, "length": length}))
    copy = message.model_copy(update={"text": rest, "entities": new_entities})
    copy.as_(message.bot)
    return copy


async def bang_middleware(handler, event: Message, data):
    """ Команды пишутся через «!». Срезаем «!» и помечаем data["bang"]=True,
        чтобы фильтр cmd_filter пропустил команду. Исходный текст лежит в
        data["raw_text"] (нужен в диалоге после покупки, где текст важен целиком). """
    data["raw_text"] = event.text
    stripped = split_bang(event)
    if stripped is not None:
        data["bang"] = True
        return await handler(stripped, data)
    data["bang"] = False
    return await handler(event, data)


def cmd_filter(*words, no_bang=()):
    """ Фильтр текстовых команд (без учёта регистра), команда в начале строки.
        Срабатывает только для сообщений с «!» впереди (см. bang_middleware).
        no_bang: слова, которые работают и без «!», если сообщение начинается
        с этого слова («Фарм», «Фарм дзо», «фарм сп»). Если слово стоит не первым
        («я фарм», «фармить»), команда не срабатывает. """
    pattern = re.compile(r"(?i)^(" + "|".join(words) + r")(\s|$)")
    free = (re.compile(r"(?i)^(" + "|".join(no_bang) + r")(\s|[.!?]*$)")
            if no_bang else None)

    def check(message: Message, bang: bool = False) -> bool:
        text = message.text
        if not text:
            return False
        if bang:
            return bool(pattern.match(text))
        return bool(free and free.match(text.strip()))

    return check


# Рассылка этого бота управляется только командами «!реклама фарм ...»
# («!реклама фарм дзо ...», «фарм-дзо», «фармдзо» тоже). Обычные «!реклама текст ...»
# и «!реклама грин маркет ...» принадлежат первому боту группы (рассылки), этот бот
# на них молчит, чтобы на одну команду не отвечали два бота.
AD_FARM_RE = re.compile(r"(?i)^(?:реклама|рассылка)\s+фарм(?:\s*-?\s*(?:дзо|dzo))?(?=\s|$)")


# Любое слово, начинающееся с «фарм», первый бот считает чужим и молчит
# («!реклама фарм, стоп», «!реклама фармер»). Если такая команда не разобрана
# AD_FARM_RE, этот бот отвечает справкой, чтобы ответил ровно один бот.
AD_FARM_LOOSE_RE = re.compile(r"(?i)^(?:реклама|рассылка)\s+фарм\S*")
_AD_ANY_RE = re.compile(r"(?i)^(?:реклама|рассылка)(?:\s|$)")
_AD_CAPTION_RE = re.compile(r"^\s*!\s*")


def ad_caption_command(message: Message):
    """ Подпись к фото вида «!реклама фарм ...»: возвращает совпадение AD_FARM_RE
        (или AD_FARM_LOOSE_RE) по подписи без «!» и саму подпись без «!», иначе None. """
    caption = message.caption
    if not caption or not message.photo:
        return None
    bang = _AD_CAPTION_RE.match(caption)
    if not bang or not caption[bang.end():].strip():
        return None
    body = caption[bang.end():]
    if not AD_FARM_LOOSE_RE.match(body):
        return None
    return AD_FARM_RE.match(body), body


def not_ad_command(message: Message, bang: bool = False) -> bool:
    """ Фильтр для диалога после покупки: команды «!реклама ...» / «!рассылка ...»
        (и фото с такой подписью) в него не попадают. «!реклама фарм ...» уходит
        в обработчики рекламы этого бота, остальные «!реклама ...» принадлежат
        первому боту группы, здесь молчим. """
    if bang and message.text and _AD_ANY_RE.match(message.text):
        return False
    # «!забрать» владельца тоже не ответ в диалоге покупки (не титул, не закреп, не мут)
    if bang and message.text and _TAKE_WORD_RE.match(message.text):
        return False
    caption = message.caption or ""
    bang_cap = _AD_CAPTION_RE.match(caption)
    if bang_cap and _AD_ANY_RE.match(caption[bang_cap.end():]):
        return False
    return True


def matched_word(text: str, words) -> str:
    for w in words:
        if re.match(rf"(?i)^{w}(\s|$)", text):
            return text[:len(w)]
    return words[0]


# ----------------------------------------------------------------------------
# Меню, магазин, настройки — клавиатуры и тексты
# ----------------------------------------------------------------------------
def menu_kb(is_admin_user: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📜 Информация", callback_data="menu:info")],
        [InlineKeyboardButton(text="📖 Инструкция", callback_data="menu:help")],
        [InlineKeyboardButton(text="🛍 Магазин товаров", callback_data="menu:shop")],
        [InlineKeyboardButton(text="🎣 Рыболовный магазин", callback_data="menu:fishshop")],
        [InlineKeyboardButton(text="💼 Бизнесы", callback_data="menu:biz")],
        [InlineKeyboardButton(text="🎒 Инвентарь", callback_data="menu:inv")],
        [InlineKeyboardButton(text="💰 Кошелёк", callback_data="menu:wallet")],
        [InlineKeyboardButton(text="🔗 Пригласить друга", callback_data="menu:invite")],
    ]
    if is_admin_user:
        rows.append([InlineKeyboardButton(text="⚙️ Настройки", callback_data="menu:settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def dzo_button(text: str, callback_data: str) -> InlineKeyboardButton:
    """ Кнопка с ценой в DZO: перед текстом золотая монета (кастомный эмодзи
        через icon_custom_emoji_id), в самом тексте символа монеты нет.
        Если Telegram откажет в иконке, emoji_fallback_middleware повторит
        отправку без неё, поставив в начало текста 🟡. """
    if not USE_CUSTOM_EMOJI:
        return InlineKeyboardButton(text=f"{DZO_EMOJI_PLAIN} {text}", callback_data=callback_data)
    return InlineKeyboardButton(
        text=text, callback_data=callback_data, icon_custom_emoji_id=DZO_EMOJI_ID,
    )


def withdraw_kb(user_id: int) -> InlineKeyboardMarkup:
    """ Кнопка «ВЫВОД ДЗО» под результатом фарма и под кошельком, видна всегда
        (нажать может только этот участник; вывод возможен от WITHDRAW_MIN). """
    return InlineKeyboardMarkup(inline_keyboard=[[
        dzo_button("ВЫВОД ДЗО", f"wd:{user_id}")
    ]])


def decision_kb(withdrawal_id: int) -> InlineKeyboardMarkup:
    """ Кнопки решения по заявке на вывод (для владельца чата). """
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Вывод одобрен", callback_data=f"wda:{withdrawal_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"wdr:{withdrawal_id}"),
    ]])


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:home")]]
    )


def shop_kb(chat_id: int, with_back: bool = False) -> InlineKeyboardMarkup:
    rows = []
    for key, item in SHOP_ITEMS.items():
        rows.append([dzo_button(
            f"{item['name']} — {get_price(chat_id, key)} {CURRENCY}", f"buy:{key}",
        )])
    if with_back:
        rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def shop_text(chat_id: int) -> str:
    lines = ["🛍 <b>Магазин товаров</b>\nНажмите на товар, чтобы купить:\n"]
    for key, item in SHOP_ITEMS.items():
        lines.append(
            f"{item['emoji']} <b>{item['name']}</b> — {cp(get_price(chat_id, key))}\n"
            f"   <i>{item['desc']}</i>"
        )
    return "\n".join(lines)


# ---- Рыболовный магазин ----------------------------------------------------
def fishshop_kb(chat_id: int, user_id: int, with_back: bool = False) -> InlineKeyboardMarkup:
    rows = []
    for key, g in GEAR.items():
        rows.append([dzo_button(
            f"{g['name']} — {get_price(chat_id, key)} {CURRENCY}", f"buy:{key}",
        )])
    # Продажа своего улова: показываем только то, что реально есть.
    inv = get_inventory(chat_id, user_id)
    for key, f in FISH.items():
        qty = inv.get(key, 0)
        if qty > 0 and f["price"] is not None and get_price(chat_id, key) > 0:
            rows.append([dzo_button(
                f"Продать {f['name']} ×{qty} — {get_price(chat_id, key)} {CURRENCY}/шт",
                f"sell:{key}",
            )])
    if with_back:
        rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def fishshop_text(chat_id: int) -> str:
    lines = ["🎣 <b>Рыболовный магазин</b>\n", "<b>Снаряжение:</b>"]
    for key, g in GEAR.items():
        lines.append(
            f"{g['emoji']} <b>{g['name']}</b> — {cp(get_price(chat_id, key))}\n"
            f"   <i>{g['desc']}</i>"
        )
    lines.append("\n<b>Скупка улова:</b>")
    for key, f in FISH.items():
        price = get_price(chat_id, key)
        if price > 0:
            lines.append(f"{f['emoji']} {f['name']} — {cp(price)}")
    lines.append(
        f"\n🥾 <i>Старый сапог ничего не стоит и ломает удочку.</i>\n"
        f"⏳ <i>После поломки удочки новую можно купить через "
        f"{human_duration(FISH_COOLDOWN)}.</i>\n"
        f"Ловить: <code>!рыбачить</code> • Продать: <code>!продать мойва</code>"
    )
    return "\n".join(lines)


# ---- Бизнесы ---------------------------------------------------------------
def business_kb(chat_id: int, with_back: bool = False) -> InlineKeyboardMarkup:
    rows = []
    for key, b in BUSINESSES.items():
        if get_business(chat_id, key):
            continue  # занят — купить нельзя
        rows.append([dzo_button(
            f"Купить {b['name']} — {get_price(chat_id, 'biz_' + key)} {CURRENCY}",
            f"bizbuy:{key}",
        )])
    if with_back:
        rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def business_text(chat_id: int) -> str:
    lines = ["💼 <b>Бизнесы</b>\nВладелец бизнеса получает процент с каждой покупки.\n"]
    for key, b in BUSINESSES.items():
        row = get_business(chat_id, key)
        price = get_price(chat_id, "biz_" + key)
        share = int(b["share"] * 100)
        if row:
            owner_id, expires = row
            left = human_duration(max(0, expires - now_ts()))
            name = get_display_name(chat_id, owner_id)
            status = f"занят: <a href=\"tg://user?id={owner_id}\">{esc(name)}</a> (ещё {left})"
        else:
            status = f"свободен — {cp(price)} на неделю"
        lines.append(f"{b['emoji']} <b>{b['name']}</b> — {share}% с покупок\n   {status}")
    lines.append(
        "\n<i>Покупка — кнопкой ниже. Аукцион запускает владелец чата "
        "командой</i> <code>!аукцион Рыболовный магазин</code>"
    )
    return "\n".join(lines)


def inventory_text(chat_id: int, user_id: int) -> str:
    inv = get_inventory(chat_id, user_id)
    lines = ["🎒 <b>Инвентарь</b>\n"]
    rod, worms = inv.get("rod", 0), inv.get("worms", 0)
    lines.append(f"{GEAR['rod']['emoji']} Удочка: <b>{rod}</b> рыбалок")
    lines.append(f"{GEAR['worms']['emoji']} Черви: <b>{worms}</b> рыбалок")
    cd = inv.get("rod_cd", 0)
    if cd > now_ts():
        lines.append(f"⏳ Новая удочка будет доступна через: "
                     f"<b>{human_duration(cd - now_ts())}</b>")
    caught = [(k, inv.get(k, 0)) for k in FISH if inv.get(k, 0) > 0]
    lines.append("\n<b>Улов:</b>")
    if caught:
        for key, qty in caught:
            f = FISH[key]
            lines.append(f"{f['emoji']} {f['name']} — <b>{qty}</b> шт")
    else:
        lines.append("<i>пусто, напишите «!рыбачить»</i>")
    return "\n".join(lines)


def settings_text(chat_id: int) -> str:
    lines = ["⚙️ <b>Настройки</b> (только для админов/владельца)\n",
             "<b>Магазин товаров:</b>"]
    for key, item in SHOP_ITEMS.items():
        lines.append(f"• {item['name']} — <b>{cp(get_price(chat_id, key))}</b>")
    lines.append("\n<b>Рыболовный магазин:</b>")
    for key, g in GEAR.items():
        lines.append(f"• {g['name']} — <b>{cp(get_price(chat_id, key))}</b>")
    for key, f in FISH.items():
        if DEFAULT_PRICES.get(key):
            lines.append(f"• {f['name']} — <b>{cp(get_price(chat_id, key))}</b>")
    lines.append("\n<b>Бизнесы:</b>")
    for key, b in BUSINESSES.items():
        lines.append(f"• {b['name']} — <b>{cp(get_price(chat_id, 'biz_' + key))}</b> / неделя")
    lines.append(f"\n🎁 Бонус за приглашение — <b>{cp(get_invite_bonus(chat_id))}</b>")
    lines.append(
        f"💰 Мин. ставка на аукционе — "
        f"<b>{cp(get_setting(chat_id, 'min_bid', DEFAULT_MIN_BID))}</b>"
    )
    lines.append(
        "\n<b>Команды:</b>\n"
        "<code>!цена [товар] [сумма]</code> — цена товара/рыбы/снасти/бизнеса\n"
        "<code>!цена рефералка 100</code> — реферальный бонус\n"
        "<code>!минимальная ставка 1000</code> — взнос на аукционе\n"
        "<code>!аукцион Рыболовный магазин</code> — начать торги"
    )
    return "\n".join(lines)


def build_dispatcher(bot: Bot, cfg: dict) -> tuple[Dispatcher, Broadcaster]:
    dp = Dispatcher()
    bot.session.middleware(emoji_fallback_middleware)
    bc = Broadcaster(bot, cfg)

    # Запоминаем каждого, кто пишет в группе (строим карту @ник -> id).
    # Нужно, чтобы «@ник» резолвился в муте/переводе/выдаче без обращения к Telegram.
    async def remember_middleware(handler, event: Message, data):
        u = event.from_user
        if u and event.chat.type in ("group", "supergroup"):
            key = (event.chat.id, u.id)
            val = (u.full_name, u.username)
            if _SEEN_USERS.get(key) != val:   # пишем в БД только при изменении
                _SEEN_USERS[key] = val
                remember_user(event.chat.id, u)
        return await handler(event, data)

    dp.message.outer_middleware(remember_middleware)
    dp.message.outer_middleware(bang_middleware)

    # ========================================================================
    #                        ЭКОНОМИКА DZO
    # ========================================================================

    async def schedule_unpin(chat_id: int, message_id: int, delay: int):
        try:
            await asyncio.sleep(delay)
            await bot.unpin_chat_message(chat_id, message_id)
        except Exception:  # noqa: BLE001
            pass

    async def notify_owner(message: Message, buyer, item: dict):
        """ Ручной товар: выделяем владельца в чате. """
        owner_tag = "Владелец"
        try:
            admins = await bot.get_chat_administrators(message.chat.id)
            creator = next(
                (a.user for a in admins if a.status == ChatMemberStatus.CREATOR), None
            )
            if creator:
                owner_tag = mention(creator)
        except TelegramBadRequest:
            pass
        await message.answer(
            f"👑 <b>Внимание, Владелец!</b> {owner_tag}\n"
            f"▫️ Пользователь {mention(buyer)} приобрёл товар "
            f"«<b>{item['name']}</b>». Ожидает вашего решения."
        )

    def pay_share(chat_id: int, business: str, price: int) -> None:
        """ Отчисляет владельцу бизнеса его процент с покупки (молча). """
        row = get_business(chat_id, business)
        if not row:
            return
        share = int(price * BUSINESSES[business]["share"])
        if share > 0:
            add_balance(chat_id, row[0], share)

    async def process_purchase(cb: CallbackQuery, key: str):
        if not cb.message:
            await cb.answer("Товар не найден.", show_alert=True)
            return
        chat = cb.message.chat
        if chat.type not in ("group", "supergroup"):
            await cb.answer("Покупки доступны только в группе.", show_alert=True)
            return
        item = SHOP_ITEMS.get(key) or GEAR.get(key)
        if not item:
            await cb.answer("Товар не найден.", show_alert=True)
            return
        buyer = cb.from_user
        price = get_price(chat.id, key)
        bal = get_balance(chat.id, buyer.id)
        if bal < price:
            await cb.answer(
                f"Недостаточно DZO.\nНужно: {price}, у вас: {bal}.", show_alert=True
            )
            return
        if key in SHOP_ITEMS and (chat.id, buyer.id) in PENDING:
            await cb.answer(
                "Сначала завершите предыдущую покупку (или напишите «отмена»).",
                show_alert=True,
            )
            return
        # --- Проверки для рыболовного снаряжения (до списания DZO!) ---
        if key in GEAR:
            have = get_item_qty(chat.id, buyer.id, key)
            if have > 0:
                # покупка «выставляет» 10, поэтому докупать поверх — просто потеря DZO
                await cb.answer(
                    f"{item['emoji']} «{item['name']}» уже есть — хватит ещё на "
                    f"{have} рыбалок.\nДокупать можно только когда закончатся.",
                    show_alert=True,
                )
                return
            cd = get_item_qty(chat.id, buyer.id, "rod_cd")
            if cd > now_ts():
                left = human_duration(cd - now_ts())
                await cb.answer(
                    f"🎣 Удочка сломана — рыбалка будет доступна через {left}."
                    + ("" if key == "rod" else "\nЧерви пока покупать бессмысленно."),
                    show_alert=True,
                )
                return

        add_balance(chat.id, buyer.id, -price, buyer)  # списываем сразу
        await cb.answer(f"✅ Куплено за {cp_plain(price)}!")

        # --- Рыболовное снаряжение ---
        if key in GEAR:
            pay_share(chat.id, "fishshop", price)
            set_item(chat.id, buyer.id, key, FISH_USES)
            await cb.message.answer(
                f"{item['emoji']} {mention(buyer)} купил «<b>{item['name']}</b>» — "
                f"хватит на {FISH_USES} рыбалок.\nПишите <code>!рыбачить</code>! 🐟"
            )
            return

        # --- Магазин товаров ---
        pay_share(chat.id, "shop", price)
        kind = item["kind"]
        if kind == "pin":
            PENDING[(chat.id, buyer.id)] = {
                "kind": "pin", "price": price, "ts": now_ts(),
                "secs": item["secs"], "name": item["name"],
            }
            await cb.message.answer(
                f"📌 {mention(buyer)}, отправьте <b>текст или медиа</b> (фото/скрин) "
                f"для закрепления на {human_duration(item['secs'])}.\n"
                f"<i>Напишите «отмена», чтобы вернуть DZO.</i>"
            )
        elif kind == "mute":
            PENDING[(chat.id, buyer.id)] = {"kind": "mute", "price": price, "ts": now_ts()}
            await cb.message.answer(
                f"🔇 {mention(buyer)}, кого мутим на 10 минут?\n"
                f"👉 Самый надёжный способ — <b>ответить (reply)</b> на сообщение этого "
                f"пользователя (можно просто точкой «.»).\n"
                f"Также можно прислать @ник (сработает, если он писал в чате).\n"
                f"<i>Напишите «отмена», чтобы вернуть DZO.</i>"
            )
        elif kind == "tag":
            PENDING[(chat.id, buyer.id)] = {"kind": "tag", "price": price, "ts": now_ts()}
            await cb.message.answer(
                f"🏷 {mention(buyer)}, пришлите <b>текст вашего титула</b> "
                f"(до 32 символов) — он будет виден рядом с вашим именем.\n"
                f"<i>Напишите «отмена», чтобы вернуть DZO.</i>"
            )
        else:  # manual: dzo / mod / ad
            await notify_owner(cb.message, buyer, item)

    # ----- Ожидание данных после покупки (высший приоритет) -----
    @dp.message(F.func(lambda m: bool(m.from_user) and (m.chat.id, m.from_user.id) in PENDING),
                not_ad_command)
    async def pending_cmd(message: Message, raw_text: str | None = None):
        key = (message.chat.id, message.from_user.id)
        state = PENDING.get(key)
        if not state:
            return
        # Исходный текст, как его написал участник (с «!», если он был).
        original = raw_text if raw_text is not None else message.text

        # истёк срок ожидания — возвращаем DZO
        if now_ts() - state["ts"] > PENDING_TTL:
            PENDING.pop(key, None)
            add_balance(message.chat.id, message.from_user.id, state["price"], message.from_user)
            await message.reply("⌛ Время на покупку истекло. DZO возвращены.")
            return

        # «отмена» — ответ внутри диалога покупки, работает и с «!», и без него.
        if (original or "").strip().lower().lstrip("!").strip() in ("отмена", "cancel"):
            PENDING.pop(key, None)
            add_balance(message.chat.id, message.from_user.id, state["price"], message.from_user)
            await message.reply("↩️ Покупка отменена, DZO возвращены.")
            return

        if state["kind"] == "pin":
            secs = state.get("secs", 24 * 3600)
            try:
                await bot.pin_chat_message(message.chat.id, message.message_id)
                PENDING.pop(key, None)
                asyncio.create_task(
                    schedule_unpin(message.chat.id, message.message_id, secs)
                )
                await message.reply(f"📌 Сообщение закреплено на {human_duration(secs)}!")
            except TelegramBadRequest as e:
                PENDING.pop(key, None)
                add_balance(message.chat.id, message.from_user.id, state["price"], message.from_user)
                await message.reply(
                    f"Не удалось закрепить (нет прав у бота?): {e.message}\nDZO возвращены."
                )

        elif state["kind"] == "tag":
            title = (original or "").strip()
            if not title:
                await message.reply(
                    "Пришлите титул <b>текстом</b>.\n<i>«отмена» — вернуть DZO.</i>"
                )
                return  # оставляем ожидание активным
            title = title[:32]
            set_title(message.chat.id, message.from_user.id, title)
            PENDING.pop(key, None)
            await message.reply(
                f"🏷 Титул установлен: <b>{esc(title)}</b>\n"
                f"Теперь вы — {titled(message.chat.id, message.from_user)}"
            )

        elif state["kind"] == "mute":
            target = await resolve_target(message, numeric=True)
            if not target:
                await message.reply(
                    "Не понял, кого мутить 🤔\n"
                    "👉 Надёжнее всего — <b>ответить (reply)</b> на сообщение нужного "
                    "человека (хоть точкой «.»). @ник срабатывает, только если он уже "
                    "писал в чате.\n<i>«отмена» — вернуть DZO.</i>"
                )
                return  # оставляем ожидание активным
            try:
                # Суммирование: если человек уже в муте — продлеваем, а не перезаписываем.
                base = now_ts()
                try:
                    cm = await bot.get_chat_member(message.chat.id, target.id)
                    until_cur = getattr(cm, "until_date", None)
                    if until_cur:
                        cur_ts = int(until_cur.timestamp()) if hasattr(until_cur, "timestamp") \
                            else int(until_cur)
                        base = max(base, cur_ts)
                except TelegramBadRequest:
                    pass
                new_until = base + MUTE_DURATION
                await bot.restrict_chat_member(
                    message.chat.id, target.id,
                    permissions=MUTE_PERMISSIONS,
                    until_date=datetime.fromtimestamp(new_until, timezone.utc),
                )
                PENDING.pop(key, None)
                left = human_duration(max(0, new_until - now_ts()))
                await message.reply(
                    f"🔇 {mention(target)} замучен (+10 минут).\n"
                    f"Всего осталось: <b>{left}</b>."
                )
            except TelegramBadRequest as e:
                PENDING.pop(key, None)
                add_balance(message.chat.id, message.from_user.id, state["price"], message.from_user)
                await message.reply(f"Не удалось замутить: {e.message}\nDZO возвращены.")

    # ----- Персональная ссылка-приглашение -----
    async def get_or_create_ref_link(chat_id: int, user) -> str:
        """ Возвращает персональную ссылку пользователя, создавая её при необходимости. """
        link = get_user_link(chat_id, user.id)
        if link:
            return link
        invite = await bot.create_chat_invite_link(chat_id, name=f"ref_{user.id}"[:32])
        save_invite_link(chat_id, invite.invite_link, user.id)
        return invite.invite_link

    # ----- Бонус за приглашение друга -----
    async def award_invite(chat_id: int, adder, new_user):
        """ Начисляет пригласившему реф-бонус один раз за нового участника. """
        if not adder or not new_user or new_user.is_bot:
            return
        if adder.is_bot or adder.id == new_user.id:
            return  # зашёл сам (по ссылке) — бонуса нет
        if was_invited(chat_id, new_user.id):
            return  # этот пользователь уже приносил бонус когда-то
        bonus = get_invite_bonus(chat_id)
        mark_invited(chat_id, new_user.id)
        bal = add_balance(chat_id, adder.id, bonus, adder)
        await bot.send_message(
            chat_id,
            f"🎉 {mention(adder)} пригласил {mention(new_user)} и получил "
            f"{cp(bonus)} на баланс!\n💰 Баланс: <b>{cp(bal)}</b>",
        )

    # Способ 1: сервисное сообщение «X добавил Y» (работает и без прав админа у бота).
    @dp.message(F.new_chat_members)
    async def on_join(message: Message):
        for u in message.new_chat_members or []:
            log.info(
                "Вступление (сервисное): чат=%s, добавил=%s, новый=%s",
                message.chat.id, getattr(message.from_user, "id", None), u.id,
            )
            remember_user(message.chat.id, u)
            await award_invite(message.chat.id, message.from_user, u)

    # Способ 2: обновление статуса участника (надёжнее; требует, чтобы бот был админом).
    @dp.chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
    async def on_member_join(event: ChatMemberUpdated):
        new_user = event.new_chat_member.user
        used_link = event.invite_link.invite_link if event.invite_link else None
        log.info(
            "Вступление (chat_member): чат=%s, кто=%s, новый=%s, ссылка=%s, статус %s->%s",
            event.chat.id, getattr(event.from_user, "id", None), new_user.id, used_link,
            event.old_chat_member.status, event.new_chat_member.status,
        )
        remember_user(event.chat.id, new_user)
        # 1) Вход по персональной ссылке → бонус владельцу ссылки.
        if used_link:
            owner_id = get_link_owner(event.chat.id, used_link)
            if owner_id and owner_id != new_user.id:
                try:
                    inviter = (await bot.get_chat_member(event.chat.id, owner_id)).user
                except TelegramBadRequest:
                    inviter = None
                if inviter:
                    await award_invite(event.chat.id, inviter, new_user)
                    return
        # 2) Прямое добавление участником.
        await award_invite(event.chat.id, event.from_user, new_user)

    # ----- ССЫЛКА-ПРИГЛАШЕНИЕ (получить свою) -----
    @dp.message(cmd_filter("пригласить", "моя ссылка", "ссылка приглашение", "ссылка"))
    async def invite_link_cmd(message: Message):
        if not is_group(message):
            await message.reply("⚠️ Ссылку-приглашение можно получить только в группе.")
            return
        try:
            link = await get_or_create_ref_link(message.chat.id, message.from_user)
        except TelegramBadRequest as e:
            await message.reply(
                "Не удалось создать ссылку. Бот должен быть админом с правом "
                f"«Пригласить пользователей».\n<code>{e.message}</code>"
            )
            return
        await message.reply(
            f"🔗 {mention(message.from_user)}, твоя персональная ссылка-приглашение "
            f"(нажми, чтобы скопировать):\n"
            f"<code>{link}</code>\n\n"
            f"Приглашай друзей — за каждого нового участника получишь "
            f"<b>+{cp(get_invite_bonus(message.chat.id))}</b>!",
            disable_web_page_preview=True,
        )

    # Способ 3 (для проверки): админ вручную проверяет, что начисление работает.
    # Повторяемо, не помечает участника «приглашённым» — только тест механики.
    @dp.message(cmd_filter("тест инвайт", "тест приглашение"))
    async def test_invite_cmd(message: Message):
        if not await guard(message):
            return
        adder = message.from_user
        invited = None
        if message.reply_to_message and message.reply_to_message.from_user:
            invited = message.reply_to_message.from_user
        bonus = get_invite_bonus(message.chat.id)
        bal = add_balance(message.chat.id, adder.id, bonus, adder)
        who = mention(invited) if invited else "тестового участника"
        await message.answer(
            f"🧪 <b>Тест приглашения</b>\n"
            f"🎉 {mention(adder)} «пригласил» {who} и получил {cp(bonus)}!\n"
            f"💰 Баланс: <b>{cp(bal)}</b>\n"
            f"<i>Механика работает. Реальный бонус сработает при добавлении "
            f"нового участника (бот должен быть админом).</i>"
        )

    # ----- СБРОС СВОИХ приглашений (любой участник; временно, для теста) -----
    @dp.message(cmd_filter(
        "сбросить мои приглашения", "сброс моих приглашений",
        "сбросить приглашение", "сбросить приглашения", "сброс инвайт",
    ))
    async def reset_my_invite_cmd(message: Message):
        if message.from_user.id not in RESET_INVITE_ALLOWED:
            return  # команда доступна только тестировщикам из белого списка
        if not is_group(message):
            await message.reply("⚠️ Команда работает только в группе.")
            return
        reset_invited(message.chat.id, message.from_user.id)
        await message.answer(
            f"🧹 {mention(message.from_user)}, твоя история приглашений сброшена.\n"
            f"Теперь, если ты снова зайдёшь по чьей-то ссылке, пригласивший получит "
            f"<b>+{cp(get_invite_bonus(message.chat.id))}</b>."
        )

    # ----- ФАРМ -----
    # «Фарм» работает и без «!»: любое сообщение, которое начинается со слова «фарм»
    # («Фарм», «Фарм дзо», «фарм сп»), как в прежней версии бота.
    @dp.message(cmd_filter("фарм", no_bang=("фарм",)))
    async def farm_cmd(message: Message):
        if not is_group(message):
            await message.reply("⚠️ Фармить DZO можно только в группе.")
            return
        cid, user = message.chat.id, message.from_user
        last = get_farm_time(cid, user.id)
        left = FARM_COOLDOWN - (now_ts() - last)
        if last and left > 0:
            await message.reply(
                f"⏳ Рано! Следующий фарм через <b>{human_duration(left)}</b>.",
                reply_markup=withdraw_kb(user.id),
            )
            return
        amount = farm_amount()
        set_farm_time(cid, user.id, now_ts())
        bal = add_balance(cid, user.id, amount, user)
        text = (
            f"🧑‍🌾 {titled(cid, user)} получил <b>{cp(amount)}</b>! "
            f"Следующий фарм через {human_duration(FARM_COOLDOWN)}.\n"
            f"💰 Баланс: <b>{cp(bal)}</b>"
        )
        # Кнопка «ВЫВОД ДЗО» видна всегда; при балансе меньше WITHDRAW_MIN
        # нажатие только показывает предупреждение (см. withdraw_cb).
        if bal >= WITHDRAW_MIN:
            text += "\n💸 Можно вывести DZO, нажмите кнопку ниже."
        else:
            text += f"\n🎯 До вывода осталось <b>{cp(WITHDRAW_MIN - bal)}</b>."
        await message.reply(text, reply_markup=withdraw_kb(user.id))

    # ----- ВЫВОД DZO -----
    async def find_chat_owner(chat_id: int):
        """ Создатель (владелец) чата либо None. """
        try:
            admins = await bot.get_chat_administrators(chat_id)
        except TelegramAPIError:
            return None
        return next((a.user for a in admins if a.status == ChatMemberStatus.CREATOR), None)

    async def drop_button(msg) -> None:
        """ Снимает кнопки под сообщением; устаревшее/недоступное сообщение пропускаем. """
        try:
            await msg.edit_reply_markup(reply_markup=None)
        except (TelegramAPIError, AttributeError):
            pass

    async def chat_title_of(chat_id: int) -> str:
        try:
            chat = await bot.get_chat(chat_id)
            return chat.title or "чат"
        except TelegramAPIError:
            return "чат"

    async def deliver_withdrawal(wid: int):
        """ Доставляет заявку владельцу чата: в личку, а если нельзя, то в чат.
            Возвращает "dm", "chat" или None (не доставлена). Баланс не трогает.
            При успехе ставит отметку delivered. Повторная доставка той же заявки
            безопасна: решение по ней всё равно применяется один раз. """
        w = get_withdrawal(wid)
        if not w or w[6] != "pending":
            return None
        _id, cid, uid, username, name, amount, _status = w
        user = SimpleUser(uid, full_name=name or (f"@{username}" if username else f"id{uid}"),
                          username=username)
        uname = f" (@{esc(username)})" if username else ""
        request_text = (
            f"💸 <b>Заявка на вывод DZO</b> №{wid}\n"
            f"Чат: <b>{esc(await chat_title_of(cid))}</b>\n"
            f"Участник: {mention(user)}{uname}, id <code>{uid}</code>\n"
            f"Сумма: <b>{cp(amount)}</b>\n"
            f"Сумма заморожена: списана с баланса до вашего решения. "
            f"При отклонении она вернётся участнику."
        )
        owner = await find_chat_owner(cid)
        if owner:
            try:
                await bot.send_message(owner.id, request_text, reply_markup=decision_kb(wid))
            except TelegramAPIError as e:
                log.info("Владельцу нельзя написать в личку (%s), заявка уходит в чат", e)
            else:
                mark_withdrawal_delivered(wid)
                try:
                    await bot.send_message(
                        cid,
                        f"💸 {mention(user)} подал заявку на вывод <b>{cp(amount)}</b>. "
                        f"Заявка у владельца чата, сумма заморожена до решения.",
                    )
                except TelegramAPIError as e:
                    log.warning("Не удалось сообщить в чат о заявке %s: %s", wid, e)
                return "dm"
        # В чат пишем через bot.send_message: сообщение с кнопкой могло устареть
        # (старше 48 часов Telegram отдаёт его как недоступное, без методов ответа).
        owner_tag = mention(owner) if owner else "Владелец"
        try:
            await bot.send_message(
                cid,
                f"👑 {owner_tag}, новая заявка (в личку отправить не вышло, "
                f"откройте диалог с ботом).\n\n{request_text}\n\n"
                f"<i>Решение принимает только владелец чата.</i>",
                reply_markup=decision_kb(wid),
            )
        except TelegramAPIError as e:
            log.warning("Заявка на вывод %s пока не доставлена: %s", wid, e)
            return None
        mark_withdrawal_delivered(wid)
        return "chat"

    async def redeliver_pending() -> None:
        """ Досылает заявки pending без отметки доставки (после сбоя или
            перезапуска). Заявку, которую не удалось доставить дольше
            WITHDRAW_UNDELIVERED_TTL, отклоняет с возвратом суммы. """
        # Выборка может устареть, пока ждём Telegram по предыдущим заявкам
        # (участник дослал заявку кнопкой, владелец решил). Поэтому перед каждым
        # действием состояние перепроверяется, а возврат по сроку делает
        # expire_withdrawal, который проверяет всё условие атомарно.
        for wid, cid, uid, amount, created in undelivered_withdrawals():
            if now_ts() - created > WITHDRAW_UNDELIVERED_TTL:
                if expire_withdrawal(wid, now_ts() - WITHDRAW_UNDELIVERED_TTL):
                    log.warning("Заявка %s не доставлена за сутки, сумма %s возвращена", wid, amount)
                    try:
                        name = esc(get_display_name(cid, uid))
                        await bot.send_message(
                            cid,
                            f"↩️ <a href=\"tg://user?id={uid}\">{name}</a>, заявку на вывод "
                            f"<b>{cp(amount)}</b> не удалось передать владельцу, "
                            f"сумма возвращена на баланс.",
                        )
                    except TelegramAPIError:
                        pass
                continue
            if is_withdrawal_undelivered_now(wid):
                await deliver_withdrawal(wid)

    async def redelivery_loop() -> None:
        try:
            while True:
                try:
                    await redeliver_pending()
                except Exception as e:  # noqa: BLE001
                    log.warning("Повторная доставка заявок не удалась: %s", e)
                await asyncio.sleep(WITHDRAW_RETRY_EVERY)
        except asyncio.CancelledError:
            pass

    redelivery_task: dict = {}

    async def start_redelivery():
        redelivery_task["t"] = asyncio.create_task(redelivery_loop())

    async def stop_redelivery():
        t = redelivery_task.pop("t", None)
        if t and not t.done():
            t.cancel()

    dp.startup.register(start_redelivery)
    dp.shutdown.register(stop_redelivery)
    dp["redeliver_pending"] = redeliver_pending  # для тестов и ручного запуска

    @dp.callback_query(F.data.startswith("wd:"))
    async def withdraw_cb(cb: CallbackQuery):
        msg = cb.message
        if not msg or msg.chat.type not in ("group", "supergroup"):
            await cb.answer("Вывод доступен только в группе.", show_alert=True)
            return
        try:
            owner_uid = int(cb.data.split(":", 1)[1])
        except ValueError:
            await cb.answer()
            return
        user = cb.from_user
        if user.id != owner_uid:
            await cb.answer("Это не ваша кнопка.", show_alert=True)
            return
        cid = msg.chat.id
        status, wid, amount = create_withdrawal(cid, user, WITHDRAW_MIN)
        if status == "pending":
            if is_withdrawal_delivered(wid):
                await cb.answer(
                    f"Заявка на вывод {cp_plain(amount)} уже отправлена, ждите решения владельца.",
                    show_alert=True,
                )
                await drop_button(msg)
                return
            # Заявка есть, но владельцу не дошла (сбой или перезапуск бота):
            # доставляем ту же заявку заново, без новой заморозки.
            where = await deliver_withdrawal(wid)
            if where:
                await cb.answer(
                    f"Заявка на вывод {cp_plain(amount)} отправлена владельцу повторно.",
                    show_alert=True,
                )
                await drop_button(msg)
            else:
                await cb.answer(UNDELIVERED_ALERT, show_alert=True)
            return
        if status == "low":
            # Ничего не списано и заявки нет. Кнопку не снимаем: она видна всегда,
            # нажать снова можно, когда баланс дорастёт.
            await cb.answer(
                f"Для вывода нужно от {cp_plain(WITHDRAW_MIN)}. У вас {cp_plain(amount)}.",
                show_alert=True,
            )
            return

        # Заявка создана, сумма заморожена. Доставка отдельно: если она сорвётся,
        # заявка останется pending без отметки delivered и будет дослана
        # повторным нажатием или фоновым повтором (redeliver_pending).
        where = await deliver_withdrawal(wid)
        if not where:
            await cb.answer(UNDELIVERED_ALERT, show_alert=True)
            return  # кнопку не снимаем: повторное нажатие дошлёт заявку
        await cb.answer(f"Заявка на вывод {cp_plain(amount)} отправлена владельцу.")
        await drop_button(msg)

    @dp.callback_query(F.data.startswith("wda:") | F.data.startswith("wdr:"))
    async def withdraw_decision_cb(cb: CallbackQuery):
        approve = cb.data.startswith("wda:")
        try:
            wid = int(cb.data.split(":", 1)[1])
        except ValueError:
            await cb.answer()
            return
        w = get_withdrawal(wid)
        if not w:
            await cb.answer("Заявка не найдена.", show_alert=True)
            return
        _id, w_chat, w_user, w_username, w_name, w_amount, w_status = w
        owner_ok = await owner_check(bot, w_chat, cb.from_user.id)
        if owner_ok is None:
            # Telegram не ответил (например, бота удалили из группы): заявку не трогаем.
            await cb.answer(
                "Не удалось проверить, что вы владелец чата: у бота нет доступа к группе. "
                "Заявка не изменена, попробуйте позже.", show_alert=True,
            )
            return
        if not owner_ok:
            await cb.answer("Решение по выводу принимает только владелец чата.", show_alert=True)
            return
        done = decide_withdrawal(wid, approve, cb.from_user.id)
        if not done:
            final = get_withdrawal(wid)
            state = {"approved": "одобрена", "rejected": "отклонена"}.get(
                final[6] if final else "", "уже закрыта")
            await cb.answer(f"Заявка №{wid} уже {state}.", show_alert=True)
            try:
                await cb.message.edit_reply_markup(reply_markup=None)
            except (TelegramAPIError, AttributeError):
                pass
            return
        verdict = "✅ Вывод одобрен" if approve else "❌ Вывод отклонён"
        await cb.answer(verdict)
        if cb.message:
            try:
                await cb.message.edit_text(
                    f"{cb.message.html_text}\n\n<b>{verdict}</b>", reply_markup=None
                )
            except (TelegramAPIError, AttributeError):
                try:
                    await cb.message.edit_reply_markup(reply_markup=None)
                except (TelegramAPIError, AttributeError):
                    pass
        disp = esc(w_name or (f"@{w_username}" if w_username else f"id{w_user}"))
        who = f'<a href="tg://user?id={w_user}">{disp}</a>'
        if approve:
            note = f"✅ {who}, вывод оформлен: <b>{cp(w_amount)}</b>."
        else:
            bal = get_balance(w_chat, w_user)
            note = (
                f"❌ {who}, заявка на вывод <b>{cp(w_amount)}</b> отклонена владельцем. "
                f"Сумма возвращена на баланс.\n💰 Баланс: <b>{cp(bal)}</b>"
            )
        try:
            await bot.send_message(w_chat, note)
        except TelegramAPIError as e:
            log.warning("Не удалось сообщить в чат о решении по выводу: %s", e)

    # ----- КОШЕЛЁК -----
    @dp.message(cmd_filter("кошелёк", "кошелек", "баланс"))
    async def wallet_cmd(message: Message):
        user = message.from_user
        remember_user(message.chat.id, user)
        bal = get_balance(message.chat.id, user.id)
        # Кнопка «ВЫВОД ДЗО» всегда (в группе; в личке вывод не работает).
        await message.reply(
            f"💰 Кошелёк {titled(message.chat.id, user)}\nБаланс: <b>{cp(bal)}</b>",
            reply_markup=withdraw_kb(user.id) if is_group(message) else None,
        )

    # ----- ТОП -----
    @dp.message(cmd_filter("топ сп", "топ"))
    async def top_cmd(message: Message):
        if not is_group(message):
            await message.reply("⚠️ Топ доступен только в группе.")
            return
        all_rows = get_top(message.chat.id)  # все, у кого есть DZO
        if not all_rows:
            await message.reply("📊 Пока ни у кого нет DZO. Напишите «Фарм»!")
            return
        rows = all_rows[:TOP_LIMIT]
        titles = get_titles(message.chat.id)
        lines = [top_line(i, uid, bal, name, username, titles.get(uid))
                 for i, (uid, bal, name, username) in enumerate(rows)]
        shown = (f"показано {len(rows)} из {len(all_rows)}"
                 if len(all_rows) > len(rows) else f"всего: {len(rows)}")
        for chunk in top_chunks(top_header(shown), lines):
            await message.answer(chunk)

    # ----- ПЕРЕДАТЬ (P2P перевод) -----
    @dp.message(cmd_filter("передать", "перевод"))
    async def transfer_cmd(message: Message):
        if not is_group(message):
            await message.reply("⚠️ Переводы работают только в группе.")
            return
        cid, sender = message.chat.id, message.from_user
        word = matched_word(message.text, ["передать", "перевод"])
        amount = extract_amount(message.text[len(word):])
        if not amount:
            await message.reply(
                "Формат: <code>!передать 100 @ник</code> (или ответом на сообщение)."
            )
            return
        target = await resolve_target(message)
        if not target:
            await message.reply(
                "Не понял получателя 🤔 Надёжнее — <b>ответить (reply)</b> на его "
                "сообщение. @ник сработает, только если он уже писал в чате."
            )
            return
        if target.id == sender.id:
            await message.reply("🙃 Нельзя перевести самому себе.")
            return
        if getattr(target, "is_bot", False):
            await message.reply("🚫 Боту переводить нельзя.")
            return
        if amount < MIN_TRANSFER:
            await message.reply(f"Минимальная сумма перевода — {cp(MIN_TRANSFER)}.")
            return
        bal = get_balance(cid, sender.id)
        if bal < amount:
            await message.reply(f"Недостаточно средств. Баланс: <b>{cp(bal)}</b>.")
            return
        fee = int(amount * TRANSFER_FEE)
        received = amount - fee
        add_balance(cid, sender.id, -amount, sender)
        add_balance(cid, target.id, received, target)
        await message.answer(
            f"💸 {mention(sender)} передал {cp(amount)} пользователю {mention(target)}.\n"
            f"▫️ Комиссия (20%): {cp(fee)}.\n"
            f"▫️ {esc(getattr(target, 'full_name', 'Получатель'))} получил: "
            f"<b>{cp(received)}</b> на счёт."
        )

    # ----- ВЫДАТЬ (владелец начисляет DZO) -----
    @dp.message(cmd_filter("выдать"))
    async def give_cmd(message: Message):
        if not is_group(message):
            await message.reply("⚠️ Команда работает только в группе.")
            return
        if not await is_owner(bot, message.chat.id, message.from_user.id):
            await message.reply("🚫 Команда доступна только владельцу чата.")
            return
        amount = extract_amount(message.text[len("выдать"):])
        if not amount:
            await message.reply("Формат: <code>!выдать @ник 50000</code> (или ответом).")
            return
        target = await resolve_target(message, numeric=True)
        if not target:
            await message.reply("Не понял, кому выдать. Ответьте на сообщение или укажите @ник / ссылку.")
            return
        bal = add_balance(message.chat.id, target.id, amount, target)
        await message.answer(
            f"👑 Владелец начислил <b>{cp(amount)}</b> пользователю {mention(target)}.\n"
            f"💰 Баланс: <b>{cp(bal)}</b>"
        )

    # ----- ЗАБРАТЬ (владелец списывает DZO; без суммы забирает всё) -----
    @dp.message(cmd_filter("забрать"))
    async def take_cmd(message: Message):
        if not is_group(message):
            await message.reply("⚠️ Команда работает только в группе.")
            return
        if not await is_owner(bot, message.chat.id, message.from_user.id):
            await message.reply("🚫 Команда доступна только владельцу чата.")
            return
        target, amount, error = await parse_take(message)
        if error:
            await message.reply(error)
            return
        taken, bal = take_balance(message.chat.id, target.id, amount, target)
        await message.answer(
            f"👑 Владелец забрал <b>{cp(taken)}</b> у {mention(target)}.\n"
            f"💰 Баланс: <b>{cp(bal)}</b>"
        )

    # ----- МАГАЗИН ТОВАРОВ -----
    @dp.message(cmd_filter("магазин товаров", "магазин", "shop"))
    async def shop_cmd(message: Message):
        await message.answer(shop_text(message.chat.id), reply_markup=shop_kb(message.chat.id))

    # ----- РЫБОЛОВНЫЙ МАГАЗИН -----
    @dp.message(cmd_filter("рыболовный магазин", "рыбный магазин", "рыбмагазин"))
    async def fishshop_cmd(message: Message):
        await message.answer(
            fishshop_text(message.chat.id),
            reply_markup=fishshop_kb(message.chat.id, message.from_user.id),
        )

    # ----- РЫБАЧИТЬ -----
    @dp.message(cmd_filter("рыбачить", "рыбалка"))
    async def fish_cmd(message: Message):
        if not is_group(message):
            await message.reply("⚠️ Рыбачить можно только в группе.")
            return
        cid, user = message.chat.id, message.from_user
        rod = get_item_qty(cid, user.id, "rod")
        worms = get_item_qty(cid, user.id, "worms")
        if rod <= 0 or worms <= 0:
            need = []
            if rod <= 0:
                need.append(f"{GEAR['rod']['emoji']} Удочка")
            if worms <= 0:
                need.append(f"{GEAR['worms']['emoji']} Черви")
            await message.reply(
                f"🎣 Не хватает: <b>{', '.join(need)}</b>.\n"
                f"Купите в «Рыболовном магазине» — напишите "
                f"<code>!рыболовный магазин</code>."
            )
            return

        rod_left = add_item(cid, user.id, "rod", -1)
        worms_left = add_item(cid, user.id, "worms", -1)
        catch = roll_fish()
        f = FISH[catch]
        add_item(cid, user.id, catch, 1)

        if catch == "boot":
            set_item(cid, user.id, "rod", 0)  # сапог ломает удочку
            set_item(cid, user.id, "rod_cd", now_ts() + FISH_COOLDOWN)
            await message.reply(
                f"{f['emoji']} {mention(user)} выловил <b>{f['name']}</b>!\n"
                f"💔 Удочка сломалась. Новую можно купить через "
                f"<b>{human_duration(FISH_COOLDOWN)}</b>."
            )
            return

        if rod_left <= 0:
            set_item(cid, user.id, "rod_cd", now_ts() + FISH_COOLDOWN)
            tail = (
                f"\n💔 Удочка сломалась после {FISH_USES} забросов. "
                f"Новую можно купить через <b>{human_duration(FISH_COOLDOWN)}</b>."
            )
        elif worms_left <= 0:
            tail = "\n⚠️ Черви кончились — купите новых."
        else:
            tail = f"\nОсталось рыбалок: <b>{min(rod_left, worms_left)}</b>"
        await message.reply(
            f"{f['emoji']} {mention(user)} поймал <b>{f['name']}</b>! "
            f"(продать за {cp(get_price(cid, catch))}){tail}"
        )

    # ----- ИНВЕНТАРЬ -----
    @dp.message(cmd_filter("инвентарь", "рюкзак"))
    async def inventory_cmd(message: Message):
        await message.reply(inventory_text(message.chat.id, message.from_user.id))

    # ----- ПРОДАТЬ РЫБУ -----
    @dp.message(cmd_filter("продать"))
    async def sell_cmd(message: Message):
        if not is_group(message):
            await message.reply("⚠️ Продавать можно только в группе.")
            return
        cid, user = message.chat.id, message.from_user
        rest = message.text[len("продать"):].strip()
        fish_key = match_key(rest, FISH_ALIASES)
        if not fish_key:
            names = ", ".join(v["name"] for k, v in FISH.items() if DEFAULT_PRICES.get(k))
            await message.reply(
                f"Формат: <code>!продать мойва</code>\nМожно продать: {names}"
            )
            return
        f = FISH[fish_key]
        price = get_price(cid, fish_key)
        if price <= 0:
            await message.reply(f"{f['emoji']} «{f['name']}» никто не купит 🙂")
            return
        have = get_item_qty(cid, user.id, fish_key)
        if have <= 0:
            await message.reply(
                f"У вас нет «{f['name']}». Загляните в <code>!инвентарь</code>."
            )
            return
        amount = max(1, min(extract_amount(rest) or 1, have))
        add_item(cid, user.id, fish_key, -amount)
        total = price * amount
        bal = add_balance(cid, user.id, total, user)
        await message.reply(
            f"{f['emoji']} {mention(user)} продал {f['name']} ×{amount} за "
            f"<b>{cp(total)}</b>.\n💰 Баланс: <b>{cp(bal)}</b>"
        )

    # ----- БИЗНЕСЫ -----
    @dp.message(cmd_filter("бизнесы", "бизнес"))
    async def business_cmd(message: Message):
        await message.answer(
            business_text(message.chat.id),
            reply_markup=business_kb(message.chat.id),
            disable_web_page_preview=True,
        )

    # ----- АУКЦИОН (владелец чата) -----
    async def finish_auction(chat_id: int, key: str):
        a = get_auction(chat_id, key)
        if not a:
            return
        _ends, _min_bid, top_bid, top_bidder = a
        clear_auction(chat_id, key)
        b = BUSINESSES[key]
        if not top_bidder or top_bid <= 0:
            await bot.send_message(
                chat_id, f"🔨 Торги за «{b['name']}» окончены — ставок не было."
            )
            return
        if user_business(chat_id, top_bidder):
            await bot.send_message(
                chat_id,
                f"🔨 Победитель уже владеет другим бизнесом — "
                f"«{b['name']}» остался свободным.",
            )
            return
        if get_balance(chat_id, top_bidder) < top_bid:
            await bot.send_message(
                chat_id,
                f"🔨 Торги за «{b['name']}» окончены, но у победителя не хватило DZO. "
                f"Бизнес остался свободным.",
            )
            return
        add_balance(chat_id, top_bidder, -top_bid)
        set_business(chat_id, key, top_bidder, now_ts() + BUSINESS_DURATION)
        name = get_display_name(chat_id, top_bidder)
        await bot.send_message(
            chat_id,
            f"🏆 Торги окончены! {b['emoji']} «<b>{b['name']}</b>» забирает "
            f"<a href=\"tg://user?id={top_bidder}\">{esc(name)}</a> "
            f"за <b>{cp(top_bid)}</b>!\n"
            f"Бизнес на неделю, {int(b['share'] * 100)}% с каждой покупки.",
        )

    async def auction_timer(chat_id: int, key: str, delay: int):
        try:
            await asyncio.sleep(max(1, delay))
            await finish_auction(chat_id, key)
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            log.warning("Не удалось завершить аукцион: %s", e)

    async def resume_auctions():
        """ После перезапуска бота восстанавливаем таймеры незакрытых аукционов. """
        for chat_id, key, ends_ts in all_auctions():
            asyncio.create_task(auction_timer(chat_id, key, ends_ts - now_ts()))

    dp.startup.register(resume_auctions)

    @dp.message(cmd_filter("аукцион"))
    async def auction_cmd(message: Message):
        if not is_group(message):
            await message.reply("⚠️ Аукцион работает только в группе.")
            return
        if not await is_owner(bot, message.chat.id, message.from_user.id):
            await message.reply("🚫 Аукцион запускает только владелец чата.")
            return
        cid = message.chat.id
        rest = message.text[len("аукцион"):].strip()
        key = match_key(rest, BUSINESS_ALIASES)
        if not key:
            names = "\n".join(f"• {b['name']}" for b in BUSINESSES.values())
            await message.reply(
                f"Формат: <code>!аукцион Рыболовный магазин</code>\n\nБизнесы:\n{names}"
            )
            return
        if get_business(cid, key):
            await message.reply("Этот бизнес занят — дождитесь окончания недели.")
            return
        if get_auction(cid, key):
            await message.reply("Торги по этому бизнесу уже идут.")
            return
        min_bid = get_setting(cid, "min_bid", DEFAULT_MIN_BID)
        start_auction(cid, key, now_ts() + AUCTION_DURATION, min_bid)
        asyncio.create_task(auction_timer(cid, key, AUCTION_DURATION))
        b = BUSINESSES[key]
        await message.answer(
            f"🔨 <b>АУКЦИОН</b> — {b['emoji']} «<b>{b['name']}</b>»\n\n"
            f"Первоначальный взнос: <b>{cp(min_bid)}</b>\n"
            f"Длительность: <b>1 час</b>\n\n"
            f"Делайте ставки: <code>!ставка {min_bid}</code>\n"
            f"Побеждает наибольшая ставка. Победитель получает бизнес на неделю "
            f"и {int(b['share'] * 100)}% с каждой покупки в нём.\n\n"
            f"<i>DZO списываются только с победителя по окончании торгов — "
            f"тем, чью ставку перебили, терять нечего.</i>"
        )

    # ----- ОТМЕНА АУКЦИОНА (владелец) -----
    @dp.message(cmd_filter("отменить аукцион", "отмена аукциона", "стоп аукцион"))
    async def cancel_auction_cmd(message: Message):
        if not is_group(message):
            await message.reply("⚠️ Команда работает только в группе.")
            return
        if not await is_owner(bot, message.chat.id, message.from_user.id):
            await message.reply("🚫 Отменить аукцион может только владелец чата.")
            return
        cid = message.chat.id
        word = matched_word(
            message.text, ["отменить аукцион", "отмена аукциона", "стоп аукцион"]
        )
        key = match_key(message.text[len(word):], BUSINESS_ALIASES)
        if not key:  # бизнес не указан — берём единственные активные торги
            active = [k for k in BUSINESSES if get_auction(cid, k)]
            key = active[0] if len(active) == 1 else None
        if not key or not get_auction(cid, key):
            await message.reply(
                "Активных торгов не найдено.\n"
                "Формат: <code>!отменить аукцион Рыболовный магазин</code>"
            )
            return
        _ends, _min_bid, top_bid, top_bidder = get_auction(cid, key)
        clear_auction(cid, key)  # таймер сам увидит, что аукциона нет, и ничего не сделает
        note = ""
        if top_bidder:
            note = (
                f"\nЛидировала ставка <b>{cp(top_bid)}</b> — она аннулирована. "
                f"DZO никто не потерял: они списываются только с победителя "
                f"в конце торгов."
            )
        await message.answer(
            f"🚫 Аукцион за «<b>{BUSINESSES[key]['name']}</b>» отменён владельцем.{note}\n"
            f"Бизнес снова свободен для покупки."
        )

    # ----- СТАВКА -----
    @dp.message(cmd_filter("ставка"))
    async def bid_cmd(message: Message):
        if not is_group(message):
            return
        cid, user = message.chat.id, message.from_user
        active = None
        for k in BUSINESSES:
            a = get_auction(cid, k)
            if a and a[0] > now_ts():
                active = (k, a)
                break
        if not active:
            await message.reply("Сейчас нет активных торгов.")
            return
        key, (ends, min_bid, top_bid, _top_bidder) = active
        need = max(min_bid, top_bid + 1)
        amount = extract_amount(message.text[len("ставка"):])
        if not amount:
            await message.reply(f"Формат: <code>!ставка {need}</code>")
            return
        if amount < need:
            await message.reply(f"Ставка должна быть не меньше <b>{cp(need)}</b>.")
            return
        if user_business(cid, user.id):
            await message.reply("У вас уже есть бизнес — можно владеть только одним.")
            return
        bal = get_balance(cid, user.id)
        if bal < amount:
            await message.reply(f"Недостаточно DZO. Ваш баланс: <b>{cp(bal)}</b>.")
            return
        set_top_bid(cid, key, amount, user.id)
        await message.answer(
            f"🔨 {mention(user)} ставит <b>{cp(amount)}</b> за "
            f"«{BUSINESSES[key]['name']}»!\n"
            f"До конца торгов: <b>{human_duration(max(0, ends - now_ts()))}</b>"
        )

    # ----- МИНИМАЛЬНАЯ СТАВКА (админ) -----
    @dp.message(cmd_filter("минимальная ставка", "мин ставка", "взнос"))
    async def min_bid_cmd(message: Message):
        if not await guard(message):
            return
        word = matched_word(message.text, ["минимальная ставка", "мин ставка", "взнос"])
        amount = extract_amount(message.text[len(word):])
        if not amount:
            await message.reply("Формат: <code>!минимальная ставка 1000</code>")
            return
        set_setting(message.chat.id, "min_bid", amount)
        await message.answer(f"✅ Первоначальный взнос на аукционе: <b>{cp(amount)}</b>.")

    # ----- ОБНУЛИТЬ БАЛАНС (админ; старые варианты «сп обнулить» тоже работают) -----
    @dp.message(cmd_filter("сп обнулить", "обнулить сп", "обнулить"))
    async def zero_cp_cmd(message: Message):
        if not await guard(message):
            return
        target = await resolve_target(message, numeric=True) or message.from_user
        set_balance(message.chat.id, target.id, 0, target)
        await message.answer(
            f"🧹 Баланс {mention(target)} обнулён — <b>{cp(0)}</b>."
        )

    # ----- Продажа рыбы кнопкой -----
    @dp.callback_query(F.data.startswith("sell:"))
    async def sell_cb(cb: CallbackQuery):
        if not cb.message:
            await cb.answer()
            return
        cid = cb.message.chat.id
        f = FISH.get(cb.data.split(":", 1)[1])
        key = cb.data.split(":", 1)[1]
        if not f:
            await cb.answer("Не найдено.", show_alert=True)
            return
        qty = get_item_qty(cid, cb.from_user.id, key)
        if qty <= 0:
            await cb.answer("У вас нет такой рыбы.", show_alert=True)
            return
        price = get_price(cid, key)
        if price <= 0:
            await cb.answer("Это никто не купит 🙂", show_alert=True)
            return
        add_item(cid, cb.from_user.id, key, -qty)
        total = price * qty
        bal = add_balance(cid, cb.from_user.id, total, cb.from_user)
        await cb.answer(f"Продано {qty} шт за {cp_plain(total)}")
        await cb.message.answer(
            f"{f['emoji']} {mention(cb.from_user)} продал {f['name']} ×{qty} за "
            f"<b>{cp(total)}</b>.\n💰 Баланс: <b>{cp(bal)}</b>"
        )

    # ----- Покупка бизнеса кнопкой -----
    @dp.callback_query(F.data.startswith("bizbuy:"))
    async def bizbuy_cb(cb: CallbackQuery):
        if not cb.message:
            await cb.answer()
            return
        chat = cb.message.chat
        if chat.type not in ("group", "supergroup"):
            await cb.answer("Только в группе.", show_alert=True)
            return
        cid, buyer = chat.id, cb.from_user
        key = cb.data.split(":", 1)[1]
        b = BUSINESSES.get(key)
        if not b:
            await cb.answer("Не найдено.", show_alert=True)
            return
        if get_business(cid, key):
            await cb.answer("Этот бизнес уже занят.", show_alert=True)
            return
        if get_auction(cid, key):
            await cb.answer("По этому бизнесу идут торги.", show_alert=True)
            return
        own = user_business(cid, buyer.id)
        if own:
            await cb.answer(
                f"У вас уже есть бизнес: {BUSINESSES[own]['name']}.", show_alert=True
            )
            return
        price = get_price(cid, "biz_" + key)
        bal = get_balance(cid, buyer.id)
        if bal < price:
            await cb.answer(
                f"Недостаточно DZO. Нужно {price}, у вас {bal}.", show_alert=True
            )
            return
        add_balance(cid, buyer.id, -price, buyer)
        set_business(cid, key, buyer.id, now_ts() + BUSINESS_DURATION)
        await cb.answer("✅ Бизнес куплен!")
        await cb.message.answer(
            f"{b['emoji']} {mention(buyer)} купил бизнес «<b>{b['name']}</b>» "
            f"за {cp(price)} на неделю!\n"
            f"Теперь он получает <b>{int(b['share'] * 100)}%</b> с каждой покупки в нём."
        )

    # ----- ИЗМЕНЕНИЕ ЦЕНЫ (админ/владелец) -----
    @dp.message(cmd_filter("цену изменить", "цена"))
    async def price_cmd(message: Message):
        if not await guard(message):
            return
        word = matched_word(message.text, ["цену изменить", "цена"])
        rest = message.text[len(word):].strip()
        low = rest.lower()

        cid = message.chat.id

        # 1) Реферальный бонус ("Цена рефералка 100").
        if any(k in low for k in ("реферал", "приглаш", "рефка")):
            bonus = extract_amount(rest)
            if bonus is None:
                await message.reply("Формат: <code>!цена рефералка 100</code>")
                return
            set_invite_bonus(cid, bonus)
            await message.answer(f"✅ Бонус за приглашение изменён на <b>{cp(bonus)}</b>.")
            return

        price = extract_amount(rest)

        # 2) Бизнес ("Цена Рыболовный магазин 1000").
        biz = match_key(low, BUSINESS_ALIASES)
        if biz and price is not None:
            set_price(cid, "biz_" + biz, price)
            await message.answer(
                f"✅ Цена бизнеса «<b>{BUSINESSES[biz]['name']}</b>» — "
                f"<b>{cp(price)}</b> за неделю."
            )
            return

        # 3) Рыба ("Цена мойва 10").
        fish_key = match_key(low, FISH_ALIASES)
        if fish_key and price is not None:
            set_price(cid, fish_key, price)
            await message.answer(
                f"✅ {FISH[fish_key]['emoji']} Скупка «<b>{FISH[fish_key]['name']}</b>» — "
                f"<b>{cp(price)}</b> за штуку."
            )
            return

        # 4) Снасти ("Цена удочка 200").
        gear_key = match_key(low, GEAR_ALIASES)
        if gear_key and price is not None:
            set_price(cid, gear_key, price)
            await message.answer(
                f"✅ Цена «<b>{GEAR[gear_key]['name']}</b>» — <b>{cp(price)}</b>."
            )
            return

        # 5) Товары магазина ("Цену изменить Закреп на час 300").
        item_key = match_key(low, ITEM_ALIASES)
        if item_key and price is not None:
            set_price(cid, item_key, price)
            await message.answer(
                f"✅ Цена «<b>{SHOP_ITEMS[item_key]['name']}</b>» — <b>{cp(price)}</b>."
            )
            return

        goods = ", ".join(it["name"] for it in SHOP_ITEMS.values())
        fishes = ", ".join(f["name"] for f in FISH.values() if f["price"])
        gears = ", ".join(g["name"] for g in GEAR.values())
        bizs = ", ".join(b["name"] for b in BUSINESSES.values())
        await message.reply(
            "Формат: <code>!цена [название] [сумма]</code>\n"
            "Примеры: <code>!цена мойва 10</code>, <code>!цена удочка 200</code>,\n"
            "<code>!цена Рыболовный магазин 1000</code>, <code>!цена рефералка 100</code>\n\n"
            f"<b>Товары:</b> {goods}\n<b>Рыба:</b> {fishes}\n"
            f"<b>Снасти:</b> {gears}\n<b>Бизнесы:</b> {bizs}"
        )

    # ----- МЕНЮ (кнопки) -----
    @dp.message(CommandStart())
    @dp.message(cmd_filter("меню", "menu"))
    async def menu_cmd(message: Message):
        is_adm = is_group(message) and await is_admin(bot, message.chat.id, message.from_user.id)
        await message.answer(
            "🏠 <b>Главное меню</b>\nВыберите раздел:", reply_markup=menu_kb(is_adm)
        )

    # ----- ИНФОРМАЦИЯ / ИНСТРУКЦИЯ / НАСТРОЙКИ текстом -----
    @dp.message(cmd_filter("информация"))
    async def info_text_cmd(message: Message):
        await message.answer(info_text(message.chat.id), disable_web_page_preview=True)

    @dp.message(cmd_filter("инструкция"))
    async def instruction_text_cmd(message: Message):
        await message.answer(INSTRUCTION_TEXT)

    @dp.message(cmd_filter("настройки"))
    async def settings_text_cmd(message: Message):
        if not await guard(message):
            return
        await message.answer(settings_text(message.chat.id))

    # ----- Обработка нажатий кнопок меню -----
    @dp.callback_query(F.data.startswith("menu:"))
    async def menu_cb(cb: CallbackQuery):
        if not cb.message:
            await cb.answer()
            return
        section = cb.data.split(":", 1)[1]
        cid = cb.message.chat.id
        in_group = cb.message.chat.type in ("group", "supergroup")

        if section == "home":
            is_adm = in_group and await is_admin(bot, cid, cb.from_user.id)
            await cb.message.edit_text(
                "🏠 <b>Главное меню</b>\nВыберите раздел:", reply_markup=menu_kb(is_adm)
            )
            await cb.answer()
        elif section == "info":
            await cb.message.edit_text(
                info_text(cid), reply_markup=back_kb(), disable_web_page_preview=True
            )
            await cb.answer()
        elif section == "help":
            await cb.message.edit_text(INSTRUCTION_TEXT, reply_markup=back_kb())
            await cb.answer()
        elif section == "shop":
            await cb.message.edit_text(
                shop_text(cid), reply_markup=shop_kb(cid, with_back=True)
            )
            await cb.answer()
        elif section == "fishshop":
            await cb.message.edit_text(
                fishshop_text(cid),
                reply_markup=fishshop_kb(cid, cb.from_user.id, with_back=True),
            )
            await cb.answer()
        elif section == "biz":
            await cb.message.edit_text(
                business_text(cid), reply_markup=business_kb(cid, with_back=True),
                disable_web_page_preview=True,
            )
            await cb.answer()
        elif section == "inv":
            await cb.message.answer(inventory_text(cid, cb.from_user.id))
            await cb.answer()
        elif section == "wallet":
            bal = get_balance(cid, cb.from_user.id)
            await cb.answer(f"💰 Ваш баланс: {cp_plain(bal)}", show_alert=True)
        elif section == "invite":
            if not in_group:
                await cb.answer("🔗 Ссылку можно получить только в группе.", show_alert=True)
                return
            try:
                link = await get_or_create_ref_link(cid, cb.from_user)
            except TelegramBadRequest:
                await cb.answer(
                    "У бота нет права «Пригласить пользователей».", show_alert=True
                )
                return
            await cb.message.answer(
                f"🔗 {mention(cb.from_user)}, твоя ссылка-приглашение "
                f"(нажми, чтобы скопировать):\n<code>{link}</code>\n\n"
                f"За каждого нового участника — <b>+{cp(get_invite_bonus(cid))}</b>!",
                disable_web_page_preview=True,
            )
            await cb.answer()
        elif section == "settings":
            if not (in_group and await is_admin(bot, cid, cb.from_user.id)):
                await cb.answer("⚙️ Раздел только для админов.", show_alert=True)
                return
            await cb.message.edit_text(settings_text(cid), reply_markup=back_kb())
            await cb.answer()
        else:
            await cb.answer()

    # ----- Обработка кнопок «Купить» -----
    @dp.callback_query(F.data.startswith("buy:"))
    async def buy_cb(cb: CallbackQuery):
        await process_purchase(cb, cb.data.split(":", 1)[1])

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
            await message.reply("ℹ️ Ответьте на сообщение пользователя.\nПример: <code>!мут 1ч причина спам</code>")
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
        farm = AD_FARM_RE.match(message.text)
        if not farm:
            if AD_FARM_LOOSE_RE.match(message.text) and await guard(message):
                await message.reply(AD_HELP)  # «!реклама фарм, стоп» и т.п.
            return  # иначе команда первого бота группы (см. AD_FARM_RE), молчим
        if not await guard(message):
            return
        rest = message.text[farm.end():].strip()
        low = rest.lower()
        # подкоманда сравнивается целым словом («текстовая», «стопка» не команды)
        sub_m = re.match(r"\w+", low)
        sub = sub_m.group(0) if sub_m else ""
        s = bc.settings

        if sub == "текст":
            new_text = rest[len("текст"):].strip()
            if not new_text:
                await message.reply("Укажите текст: <code>!реклама фарм текст Ваше объявление</code>")
                return
            s["text"] = new_text
            bc.save()
            await message.reply("✅ Текст рекламы сохранён.")

        elif sub in ("фото", "картинка"):
            word_p = sub
            arg = rest[len(word_p):].strip().lower()
            if arg in ("удалить", "убрать", "нет", "-"):
                s["photo"] = None
                bc.save()
                await message.reply("✅ Картинка убрана — реклама снова только текстом.")
                return
            photo = None
            if message.reply_to_message and message.reply_to_message.photo:
                photo = message.reply_to_message.photo[-1].file_id
            if not photo:
                await message.reply(
                    "Ответьте командой <code>!реклама фарм фото</code> на сообщение с картинкой.\n"
                    "Убрать картинку: <code>!реклама фарм фото удалить</code>"
                )
                return
            s["photo"] = photo
            bc.save()
            await message.reply("🖼 Картинка сохранена — теперь реклама уходит вместе с ней.")

        elif sub == "интервал":
            arg = rest[len("интервал"):].strip()
            seconds = parse_duration(arg)
            if not seconds:
                await message.reply("Укажите интервал: <code>!реклама фарм интервал 2ч</code> (с/м/ч/д)")
                return
            s["interval_seconds"] = seconds
            bc.save()
            if s.get("enabled"):
                bc.start()  # перезапуск с новым интервалом
            await message.reply(f"✅ Интервал рассылки: каждые {human_duration(seconds)}.")

        elif sub in ("старт", "вкл"):
            if not s.get("text"):
                await message.reply("Сначала задайте текст: <code>!реклама фарм текст ...</code>")
                return
            s["chat_id"] = message.chat.id
            bc.start()
            await message.reply(
                f"🚀 Рассылка запущена в этой группе.\nИнтервал: {human_duration(s['interval_seconds'])}."
            )

        elif sub in ("стоп", "выкл"):
            bc.disable()
            await message.reply("🛑 Рассылка остановлена.")

        elif sub == "статус":
            state = "включена ✅" if s.get("enabled") else "выключена ❌"
            preview = (s.get("text") or "—")
            if len(preview) > 200:
                preview = preview[:200] + "…"
            await message.reply(
                f"📣 <b>Рассылка</b>: {state}\n"
                f"Интервал: {human_duration(s['interval_seconds'])}\n"
                f"Картинка: {'есть 🖼' if s.get('photo') else 'нет'}\n"
                f"Целевая группа: <code>{s.get('chat_id') or '—'}</code>\n"
                f"Текст:\n{preview}"
            )

        elif sub in ("отправить", "сейчас"):
            if s.get("text") or s.get("photo"):
                await bc.send_to(message.chat.id)
            else:
                await message.reply("Реклама не задана (нет ни текста, ни картинки).")
        else:
            await message.reply(AD_HELP)

    # ----- РЕКЛАМА: фото с подписью «!реклама фарм текст ...» / «!реклама фарм фото» -----
    @dp.message(F.photo, F.func(lambda m: ad_caption_command(m) is not None))
    async def ad_photo_caption_cmd(message: Message):
        farm, body = ad_caption_command(message)
        if not await guard(message):
            return
        rest = body[farm.end():].strip() if farm else ""
        low = rest.lower()
        sub_m = re.match(r"\w+", low)
        sub = sub_m.group(0) if sub_m else ""
        s = bc.settings
        if farm and sub == "текст":
            s["photo"] = message.photo[-1].file_id
            new_text = rest[len("текст"):].strip()
            if new_text:
                s["text"] = new_text
            bc.save()
            await message.reply("🖼 Картинка и текст рекламы сохранены." if new_text
                                else "🖼 Картинка сохранена, текст рекламы прежний.")
        elif farm and sub in ("фото", "картинка") and not rest[len(sub):].strip():
            s["photo"] = message.photo[-1].file_id
            bc.save()
            await message.reply("🖼 Картинка сохранена — теперь реклама уходит вместе с ней.")
        else:
            await message.reply(AD_HELP)

    return dp, bc


HELP_TEXT = (
    "📋 <b>Команды бота</b> (пишутся текстом через «!», без «!» работают только "
    "«Фарм» и служебная /start)\n\n"
    f"<b>{DZO_EMOJI} Экономика DZO:</b>\n"
    "• <code>!меню</code> — главное меню с кнопками\n"
    f"• <code>Фарм</code> или <code>!фарм</code> — получить {FARM_MIN}–{FARM_MAX} DZO "
    f"(раз в {FARM_COOLDOWN // 3600} часа)\n"
    f"• Кнопка «ВЫВОД ДЗО» — всегда под фармом и кошельком, вывести можно от {WITHDRAW_MIN} DZO\n"
    "• <code>!кошелёк</code> — показать баланс\n"
    "• <code>!пригласить</code> — персональная ссылка (+DZO за друга)\n"
    "• <code>!передать 100 @ник</code> — перевод (комиссия 20%)\n"
    "• <code>!магазин</code> — товары и услуги\n"
    "• <code>!топ</code> — рейтинг участников\n"
    "• <code>!сбросить мои приглашения</code> — сброс своей истории (для теста)\n\n"
    "<b>🎣 Рыбалка:</b>\n"
    "• <code>!рыболовный магазин</code> — удочка, черви, скупка улова\n"
    "• <code>!рыбачить</code> — поймать рыбу (10 раз на снасти)\n"
    "• <code>!инвентарь</code> — ваш улов и снасти\n"
    "• <code>!продать мойва</code> — продать рыбу (можно <code>!продать мойва 5</code>)\n\n"
    "<b>💼 Бизнесы:</b>\n"
    "• <code>!бизнесы</code> — список и покупка на неделю\n"
    "• <code>!ставка 1500</code> — сделать ставку на аукционе\n\n"
    "<b>👑 Админ / владелец:</b>\n"
    "• <code>!настройки</code> — текущие цены и настройки\n"
    "• <code>!цена [название] [сумма]</code> — товар / рыба / снасть / бизнес\n"
    "• <code>!цена рефералка 100</code> — реферальный бонус\n"
    "• <code>!минимальная ставка 1000</code> — взнос на аукционе\n"
    "• <code>!аукцион Рыболовный магазин</code> — начать торги (владелец)\n"
    "• <code>!отменить аукцион</code> — досрочно закрыть торги (владелец)\n"
    "• <code>!выдать @ник 50000</code> — начислить DZO (владелец)\n"
    "• <code>!забрать @ник 500</code> — забрать DZO (владелец; без суммы забирает все)\n"
    "• <code>!обнулить @ник</code> — обнулить баланс\n"
    "• Заявки на вывод DZO приходят владельцу чата в личку с ботом "
    "(если личка закрыта, то в чат), решение кнопками «Вывод одобрен» / «Отклонить»\n\n"
    "<b>🛡 Модерация:</b>\n"
    "• <code>!бан [причина]</code> — забанить\n"
    "• <code>!разбан</code> — разбанить\n"
    "• <code>!кик [причина]</code> — исключить (без блокировки)\n"
    "• <code>!мут 1ч [причина]</code> — мут на срок (с/м/ч/д)\n"
    "• <code>!размут</code> — снять мут\n"
    "• <code>!варн [причина]</code> — предупреждение (3 = автобан)\n"
    "• <code>!снять варн</code> — убрать одно предупреждение\n"
    "• <code>!варны</code> — сколько предупреждений\n"
    "• <code>!инфо</code> — данные пользователя\n\n"
    + "<b>📣 Рассылка рекламы:</b>\n" + (
        "• <code>!реклама фарм текст ...</code> — задать текст\n"
        "• <code>!реклама фарм фото</code> — ответом на картинку (задать картинку)\n"
        "• <code>!реклама фарм фото удалить</code> — убрать картинку\n"
        "• <code>!реклама фарм интервал 2ч</code> — задать интервал\n"
        "• <code>!реклама фарм старт</code> / <code>!реклама фарм стоп</code>\n"
        "• <code>!реклама фарм статус</code> — текущие настройки\n"
        "• <code>!реклама фарм отправить</code> — отправить сейчас\n"
    )
)

# Текст кнопки «Информация» (основа предоставлена заказчиком, валюта заменена на DZO).
def info_text(chat_id=None) -> str:
    """ Текст «Информации». Цены и бонус за приглашение берутся из настроек чата
        (если chat_id не задан, показываются значения по умолчанию). """
    def price(key):
        v = get_price(chat_id, key) if chat_id is not None else DEFAULT_PRICES[key]
        return f"{v:,}".replace(",", " ")

    bonus = get_invite_bonus(chat_id) if chat_id is not None else INVITE_BONUS
    return (
    "✨ <b>Добро пожаловать в наш уютный чат!</b> ✨\n\n"
    "Мы создали игровую экономику, чтобы общение стало ещё интереснее. Здесь вы "
    f"можете зарабатывать монеты <b>DZO</b> {DZO_EMOJI}, тратить их на "
    "крутые плюшки и соревноваться с друзьями!\n\n"
    "🔮 <b>Что такое DZO?</b>\n"
    "DZO это внутричатовая фарм валюта с последующим выводом. "
    "Её получают за фарм и тратят в магазине чата: закреп сообщений, мут шутников, "
    "покупка прав и даже реклама вашего клана. "
    "Под сообщением фарма и кошелька есть кнопка «ВЫВОД ДЗО». Вывести можно, "
    f"когда на балансе наберётся {WITHDRAW_MIN} DZO и больше, заявку на вывод "
    "рассматривает владелец чата.\n\n"
    "💰 <b>Как заработать DZO?</b>\n"
    f"• Каждые {FARM_COOLDOWN // 3600} часа пишите «Фарм» и получайте "
    f"от {FARM_MIN} до {FARM_MAX} DZO (везение решает!).\n"
    f"• Пригласите друга в чат — получите единоразово {bonus} DZO "
    "(бонус только за нового участника).\n"
    "• Следите за ивентами — иногда мы проводим розыгрыши с удвоением наград!\n\n"
    "🛍 <b>Как потратить DZO?</b>\n"
    "Откройте «Магазин» через главное меню и выберите товар:\n"
    f"🔹 Закреп сообщения на сутки ({price('pin')} DZO)\n"
    f"🔹 Мут надоедливого собеседника на 10 мин ({price('mute')} DZO)\n"
    "🔹 100 единиц DZO (для своих целей)\n"
    f"🔹 Роль Модератора ({price('mod')} DZO)\n"
    f"🔹 Реклама вашего клана на целые сутки ({price('ad')} DZO)\n\n"
    "🤝 <b>Переводы между участниками</b>\n"
    "Хотите поделиться? Используйте команду:\n"
    "<code>!передать [сумма] @никнейм</code>\n"
    "Но помните: в чате действует налог 20% (как в лучших банках 😄) — получатель "
    "увидит сумму уже за вычетом комиссии.\n\n"
    "📌 <b>Правила честной игры</b>\n"
    f"• Фармить можно раз в {FARM_COOLDOWN // 3600} часа, таймер у каждого участника свой.\n"
    "• За попытки читерства (выход/вход ради бонуса) — бан без права восстановления.\n"
    "• Администрация вправе корректировать курсы и цены в магазине.\n\n"
    "👑 <b>Связь с владельцем</b>\n"
    "Если у вас возникли вопросы, предложения или вы нашли баг — смело пишите "
    "создателю бота @SllowBroo. Мы всегда рады обратной связи!\n\n"
    "Приятного общения и удачного фарма! 🚀"
    )


# Текст кнопки «Инструкция»: команды, доступные всем участникам.
INSTRUCTION_TEXT = (
    "📖 <b>Инструкция</b>\n\n"
    "Все команды пишутся через «!» в начале. Без «!» работают только «Фарм» "
    "и служебная /start (открывает меню).\n\n"
    f"<b>{DZO_EMOJI} DZO:</b>\n"
    f"• <code>Фарм</code> или <code>!фарм</code> — получить {FARM_MIN}–{FARM_MAX} DZO "
    f"(раз в {FARM_COOLDOWN // 3600} часа).\n"
    f"• Кнопка «ВЫВОД ДЗО» — под фармом и кошельком, вывести можно от {WITHDRAW_MIN} DZO. "
    "Вся сумма замораживается, владелец чата одобряет или отклоняет вывод.\n"
    "• <code>!кошелёк</code> или <code>!баланс</code> — показать баланс.\n"
    "• <code>!передать [сумма] @ник</code> — перевод с комиссией 20%.\n"
    "• <code>!топ</code> — рейтинг участников по DZO.\n"
    "• <code>!пригласить</code> — личная ссылка-приглашение.\n\n"
    "<b>🛍 Магазин и рыбалка:</b>\n"
    "• <code>!магазин</code> — товары и услуги.\n"
    "• <code>!рыболовный магазин</code> — удочка, черви, скупка улова.\n"
    "• <code>!рыбачить</code> — поймать рыбу.\n"
    "• <code>!инвентарь</code> — ваш улов и снасти.\n"
    "• <code>!продать мойва 5</code> — продать рыбу.\n\n"
    "<b>💼 Бизнесы:</b>\n"
    "• <code>!бизнесы</code> — список и покупка на неделю.\n"
    "• <code>!ставка 1500</code> — ставка на аукционе.\n\n"
    "<b>ℹ️ Разное:</b>\n"
    "• <code>!меню</code> — главное меню с кнопками.\n"
    "• <code>!информация</code>, <code>!инструкция</code>, <code>!помощь</code> — справка.\n"
    "• <code>!инфо</code> — ваш ID и предупреждения.\n\n"
    "<b>Покупки:</b>\n"
    "• Закреп — бот запросит текст/медиа и закрепит.\n"
    "• Мут на 10 минут — бот спросит, кого мутить (ответьте на его сообщение).\n"
    "• Реклама клана, Модератор, 100 DZO — после покупки владелец получит уведомление.\n"
    "• Передумали во время покупки — напишите «отмена», DZO вернутся.\n\n"
    "<b>Ограничения:</b>\n"
    f"• Фарм — раз в {FARM_COOLDOWN // 3600} часа.\n"
    "• Комиссия за перевод — 20%.\n"
    f"• Минимальная сумма перевода — {MIN_TRANSFER} DZO.\n\n"
    "Вопросы? — пишите владельцу.\n\n"
    "🚀 Удачного фарма!"
)

AD_HELP = (
    "📣 <b>Управление рассылкой:</b>\n"
    "• <code>!реклама фарм текст ...</code>\n"
    "• <code>!реклама фарм фото</code> — ответом на картинку\n"
    "• <code>!реклама фарм фото удалить</code>\n"
    "• <code>!реклама фарм интервал 2ч</code>\n"
    "• <code>!реклама фарм старт</code> / <code>!реклама фарм стоп</code>\n"
    "• <code>!реклама фарм статус</code>\n"
    "• <code>!реклама фарм отправить</code> — прямо сейчас"
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
        # allowed_updates с chat_member — иначе Telegram не пришлёт события вступления
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        bc.stop()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
