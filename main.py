import os
import logging
from datetime import datetime

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL не задан")


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("fenix-report")


# ============================================================
# BOT
# ============================================================

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(
        parse_mode=ParseMode.HTML
    ),
)

dp = Dispatcher(storage=MemoryStorage())

db_pool: asyncpg.Pool | None = None


# ============================================================
# STATES
# ============================================================

class ReportForm(StatesGroup):
    target_type = State()
    category = State()
    target = State()
    description = State()
    preview = State()


# ============================================================
# DATABASE
# ============================================================

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS reports (
    id BIGSERIAL PRIMARY KEY,

    telegram_user_id BIGINT NOT NULL,
    telegram_username TEXT,

    target_type TEXT NOT NULL,
    category TEXT NOT NULL,

    target TEXT NOT NULL,
    description TEXT NOT NULL,

    generated_text TEXT NOT NULL,

    status TEXT NOT NULL DEFAULT 'DRAFT',

    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);
"""


async def init_database():
    global db_pool

    db_pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=5,
    )

    async with db_pool.acquire() as conn:
        await conn.execute(CREATE_TABLE)

    logger.info("PostgreSQL connected")


async def save_report(
    user: Message,
    target_type: str,
    category: str,
    target: str,
    description: str,
    generated_text: str,
):
    async with db_pool.acquire() as conn:
        report_id = await conn.fetchval(
            """
            INSERT INTO reports (
                telegram_user_id,
                telegram_username,
                target_type,
                category,
                target,
                description,
                generated_text,
                status
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, 'SENT')
            RETURNING id
            """,
            user.from_user.id,
            user.from_user.username,
            target_type,
            category,
            target,
            description,
            generated_text,
        )

    return report_id


# ============================================================
# KEYBOARDS
# ============================================================

def main_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👤 Аккаунт",
                    callback_data="type:account",
                ),
                InlineKeyboardButton(
                    text="📢 Канал",
                    callback_data="type:channel",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="👥 Группа",
                    callback_data="type:group",
                ),
                InlineKeyboardButton(
                    text="💬 Сообщение",
                    callback_data="type:message",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📋 Мои обращения",
                    callback_data="my_reports",
                ),
            ],
        ]
    )


def categories_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🚫 Спам",
                    callback_data="category:spam",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🎭 Выдача себя за другого",
                    callback_data="category:impersonation",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💰 Мошенничество",
                    callback_data="category:fraud",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="©️ Авторские права",
                    callback_data="category:copyright",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔞 Запрещённый контент",
                    callback_data="category:prohibited",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⚠️ Другое",
                    callback_data="category:other",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data="cancel",
                ),
            ],
        ]
    )


def preview_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Создать обращение",
                    callback_data="report:confirm",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Заполнить заново",
                    callback_data="report:restart",
                ),
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data="cancel",
                ),
            ],
        ]
    )


# ============================================================
# TEXT HELPERS
# ============================================================

TYPE_NAMES = {
    "account": "Аккаунт",
    "channel": "Канал",
    "group": "Группа",
    "message": "Сообщение",
}

CATEGORY_NAMES = {
    "spam": "Спам",
    "impersonation": "Выдача себя за другого",
    "fraud": "Мошенничество",
    "copyright": "Нарушение авторских прав",
    "prohibited": "Запрещённый контент",
    "other": "Другое",
}


def generate_report_text(
    target_type: str,
    category: str,
    target: str,
    description: str,
):
    type_name = TYPE_NAMES.get(
        target_type,
        target_type,
    )

    category_name = CATEGORY_NAMES.get(
        category,
        category,
    )

    return (
        "Здравствуйте.\n\n"
        f"Хочу сообщить о потенциальном нарушении "
        f"правил Telegram.\n\n"
        f"Тип объекта: {type_name}\n"
        f"Объект: {target}\n"
        f"Категория: {category_name}\n\n"
        f"Описание:\n{description}\n\n"
        "Прошу проверить указанный объект "
        "и принять необходимые меры, если "
        "нарушение действительно подтвердится.\n\n"
        "Спасибо."
    )


def escape_html(text: str):
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ============================================================
# /START
# ============================================================

@dp.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    await state.clear()

    await message.answer(
        "<b>🔥 FENIX REPORT</b>\n\n"
        "Система создания обращений.\n\n"
        "Выберите тип объекта:",
        reply_markup=main_menu(),
    )


# ============================================================
# TYPE
# ============================================================

@dp.callback_query(F.data.startswith("type:"))
async def type_handler(
    callback: CallbackQuery,
    state: FSMContext,
):
    target_type = callback.data.split(":", 1)[1]

    await state.update_data(
        target_type=target_type
    )

    await state.set_state(
        ReportForm.category
    )

    await callback.message.edit_text(
        "<b>⚠️ Выберите категорию нарушения</b>",
        reply_markup=categories_keyboard(),
    )

    await callback.answer()


# ============================================================
# CATEGORY
# ============================================================

@dp.callback_query(F.data.startswith("category:"))
async def category_handler(
    callback: CallbackQuery,
    state: FSMContext,
):
    category = callback.data.split(":", 1)[1]

    await state.update_data(
        category=category
    )

    await state.set_state(
        ReportForm.target
    )

    await callback.message.edit_text(
        "<b>🔗 Укажите объект</b>\n\n"
        "Отправьте:\n"
        "• @username\n"
        "• https://t.me/username\n"
        "• ссылку на сообщение\n\n"
        "Используйте только корректную ссылку "
        "или username.",
    )

    await callback.answer()


# ============================================================
# TARGET
# ============================================================

@dp.message(ReportForm.target)
async def target_handler(
    message: Message,
    state: FSMContext,
):
    target = (message.text or "").strip()

    if not target:
        await message.answer(
            "⚠️ Отправьте @username или ссылку."
        )
        return

    if len(target) > 500:
        await message.answer(
            "⚠️ Ссылка слишком длинная."
        )
        return

    await state.update_data(
        target=target
    )

    await state.set_state(
        ReportForm.description
    )

    await message.answer(
        "<b>📝 Опишите нарушение</b>\n\n"
        "Укажите только реальные обстоятельства:\n"
        "что произошло, где это произошло и "
        "почему вы считаете это нарушением."
    )


# ============================================================
# DESCRIPTION
# ============================================================

@dp.message(ReportForm.description)
async def description_handler(
    message: Message,
    state: FSMContext,
):
    description = (message.text or "").strip()

    if len(description) < 10:
        await message.answer(
            "⚠️ Опишите ситуацию подробнее."
        )
        return

    if len(description) > 4000:
        await message.answer(
            "⚠️ Описание не должно превышать 4000 символов."
        )
        return

    await state.update_data(
        description=description
    )

    data = await state.get_data()

    generated_text = generate_report_text(
        target_type=data["target_type"],
        category=data["category"],
        target=data["target"],
        description=description,
    )

    await state.update_data(
        generated_text=generated_text
    )

    await state.set_state(
        ReportForm.preview
    )

    type_name = TYPE_NAMES.get(
        data["target_type"],
        data["target_type"],
    )

    category_name = CATEGORY_NAMES.get(
        data["category"],
        data["category"],
    )

    preview = (
        "<b>📋 ПРЕДПРОСМОТР</b>\n\n"
        f"<b>Тип:</b> {escape_html(type_name)}\n"
        f"<b>Категория:</b> {escape_html(category_name)}\n"
        f"<b>Объект:</b> {escape_html(data['target'])}\n\n"
        "<b>Текст обращения:</b>\n\n"
        f"{escape_html(generated_text)}"
    )

    await message.answer(
        preview,
        reply_markup=preview_keyboard(),
    )


# ============================================================
# CONFIRM
# ============================================================

@dp.callback_query(F.data == "report:confirm")
async def confirm_report(
    callback: CallbackQuery,
    state: FSMContext,
):
    data = await state.get_data()

    if not data:
        await callback.answer(
            "Сессия устарела.",
            show_alert=True,
        )
        return

    report_id = await save_report(
        user=callback.message,
        target_type=data["target_type"],
        category=data["category"],
        target=data["target"],
        description=data["description"],
        generated_text=data["generated_text"],
    )

    await state.clear()

    await callback.message.edit_text(
        "<b>✅ Обращение создано</b>\n\n"
        f"ID обращения: <code>#{report_id}</code>\n"
        "Статус: <b>SENT</b>\n\n"
        "Обращение сохранено в PostgreSQL."
    )

    await callback.answer(
        "Обращение создано!"
    )


# ============================================================
# RESTART
# ============================================================

@dp.callback_query(F.data == "report:restart")
async def restart_report(
    callback: CallbackQuery,
    state: FSMContext,
):
    await state.clear()

    await callback.message.edit_text(
        "<b>🔥 FENIX REPORT</b>\n\n"
        "Начнём создание обращения заново.\n\n"
        "Выберите тип объекта:",
        reply_markup=main_menu(),
    )

    await callback.answer()


# ============================================================
# MY REPORTS
# ============================================================

@dp.callback_query(F.data == "my_reports")
async def my_reports(
    callback: CallbackQuery,
):
    user_id = callback.from_user.id

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                id,
                target_type,
                category,
                target,
                status,
                created_at
            FROM reports
            WHERE telegram_user_id = $1
            ORDER BY created_at DESC
            LIMIT 10
            """,
            user_id,
        )

    if not rows:
        await callback.message.edit_text(
            "<b>📋 Мои обращения</b>\n\n"
            "Обращений пока нет.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="⬅️ Назад",
                            callback_data="back:main",
                        )
                    ]
                ]
            ),
        )

        await callback.answer()
        return

    text = "<b>📋 Последние обращения</b>\n\n"

    for row in rows:
        type_name = TYPE_NAMES.get(
            row["target_type"],
            row["target_type"],
        )

        category_name = CATEGORY_NAMES.get(
            row["category"],
            row["category"],
        )

        created = row["created_at"].strftime(
            "%d.%m.%Y %H:%M"
        )

        text += (
            f"🆔 <code>#{row['id']}</code>\n"
            f"🎯 {escape_html(type_name)}\n"
            f"⚠️ {escape_html(category_name)}\n"
            f"🔗 {escape_html(row['target'])}\n"
            f"📌 {escape_html(row['status'])}\n"
            f"🕐 {created}\n\n"
        )

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⬅️ Назад",
                        callback_data="back:main",
                    )
                ]
            ]
        ),
    )

    await callback.answer()


# ============================================================
# BACK
# ============================================================

@dp.callback_query(F.data == "back:main")
async def back_main(
    callback: CallbackQuery,
    state: FSMContext,
):
    await state.clear()

    await callback.message.edit_text(
        "<b>🔥 FENIX REPORT</b>\n\n"
        "Выберите действие:",
        reply_markup=main_menu(),
    )

    await callback.answer()


# ============================================================
# CANCEL
# ============================================================

@dp.callback_query(F.data == "cancel")
async def cancel_handler(
    callback: CallbackQuery,
    state: FSMContext,
):
    await state.clear()

    await callback.message.edit_text(
        "<b>❌ Создание обращения отменено.</b>\n\n"
        "Главное меню:",
        reply_markup=main_menu(),
    )

    await callback.answer()


# ============================================================
# START BOT
# ============================================================

async def main():
    logger.info("Starting Fenix Report...")

    await init_database()

    me = await bot.get_me()

    logger.info(
        "Bot started: @%s",
        me.username,
    )

    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped")