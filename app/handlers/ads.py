from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from app.database import Database
from app.filters import BangCommand
from app.scheduler import AdScheduler
from app.utils import ensure_allowed_chat, require_admin

router = Router(name="ads")
INTERVALS = {"5м": 300, "10м": 600, "1ч": 3600, "2ч": 7200}


class AdStates(StatesGroup):
    waiting_photo = State()


async def _bot_can_post(message: Message, bot: Bot) -> bool:
    me = await bot.get_me()
    try:
        member = await bot.get_chat_member(message.chat.id, me.id)
    except TelegramAPIError:
        await message.answer("Не удалось проверить права бота в группе.")
        return False
    if (
        member.status in {"left", "kicked"}
        or getattr(member, "can_send_messages", True) is False
    ):
        await message.answer("Боту запрещено отправлять сообщения в этой группе.")
        return False
    return True


@router.message(AdStates.waiting_photo, F.photo)
async def receive_ad_photo(
    message: Message, state: FSMContext, bot: Bot, db: Database
) -> None:
    if not await require_admin(message, bot):
        return
    state_data = await state.get_data()
    if state_data.get("chat_id") != message.chat.id:
        return
    if not await ensure_allowed_chat(message, db):
        return
    caption = message.caption_html if message.caption else None
    await db.update_settings(
        message.chat.id,
        ad_photo_file_id=message.photo[-1].file_id,
        ad_caption=caption,
    )
    await state.clear()
    await message.answer("Фото и подпись рекламы сохранены.")


@router.message(AdStates.waiting_photo, BangCommand("отмена", exact=True))
async def cancel_ad_photo(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Ожидание фотографии отменено.")


@router.message(BangCommand("реклама"))
async def advertisement_command(
    message: Message,
    command_args: str,
    state: FSMContext,
    bot: Bot,
    db: Database,
    ad_scheduler: AdScheduler,
) -> None:
    if not await require_admin(message, bot):
        return
    if not await ensure_allowed_chat(message, db, bind_if_empty=True):
        return

    args = command_args.strip()
    lowered = args.lower()

    if lowered.startswith("текст "):
        text = args[6:].strip()
        if not text:
            await message.answer(
                "Использование: <code>!реклама текст &lt;текст&gt;</code>"
            )
            return
        await db.update_settings(message.chat.id, ad_text=text)
        await message.answer(
            "Текст рекламы сохранён. HTML будет применён при отправке."
        )
        return

    if lowered == "фото":
        await state.set_state(AdStates.waiting_photo)
        await state.update_data(chat_id=message.chat.id)
        await message.answer(
            "Отправьте фотографию следующим сообщением. Подпись к фото необязательна; "
            "<code>!отмена</code> отменяет ожидание."
        )
        return

    if lowered.startswith("интервал "):
        interval_name = lowered[9:].strip()
        seconds = INTERVALS.get(interval_name)
        if seconds is None:
            await message.answer("Допустимые интервалы: 5м, 10м, 1ч, 2ч.")
            return
        await db.update_settings(message.chat.id, ad_interval_seconds=seconds)
        settings = await db.get_settings(message.chat.id)
        if settings.ad_enabled:
            ad_scheduler.schedule(message.chat.id, seconds)
        await message.answer(f"Интервал рекламы установлен: {interval_name}.")
        return

    if lowered == "старт":
        settings = await db.get_settings(message.chat.id)
        if not settings.ad_text and not settings.ad_photo_file_id:
            await message.answer("Сначала задайте текст или фотографию рекламы.")
            return
        if not await _bot_can_post(message, bot):
            return
        await db.update_settings(message.chat.id, ad_enabled=True)
        ad_scheduler.schedule(message.chat.id, settings.ad_interval_seconds)
        await message.answer("Автоматическая рекламная рассылка запущена.")
        return

    if lowered == "стоп":
        await db.update_settings(message.chat.id, ad_enabled=False)
        ad_scheduler.unschedule(message.chat.id)
        await message.answer("Автоматическая рекламная рассылка остановлена.")
        return

    if lowered == "отправить":
        if not await _bot_can_post(message, bot):
            return
        try:
            sent = await ad_scheduler.send_ad(message.chat.id, raise_errors=True)
        except TelegramAPIError:
            await message.answer(
                "Telegram отклонил рекламу. Проверьте HTML, размер текста/подписи и права бота."
            )
            return
        if not sent:
            await message.answer("Реклама ещё не настроена.")
        return

    await message.answer(
        "Команды рекламы:\n"
        "<code>!реклама текст &lt;текст&gt;</code>\n"
        "<code>!реклама фото</code>\n"
        "<code>!реклама интервал 5м|10м|1ч|2ч</code>\n"
        "<code>!реклама старт</code>, <code>!реклама стоп</code>, "
        "<code>!реклама отправить</code>"
    )
