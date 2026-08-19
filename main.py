import os
import logging
import asyncio
import html
from datetime import datetime, timedelta

import asyncpg

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
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
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL не задан")

if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID не задан")


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("fenix")


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

db_pool = None


# ============================================================
# STATES
# ============================================================

class ReportForm(StatesGroup):
    target_type = State()
    category = State()
    target = State()
    description = State()


class SubscriptionForm(StatesGroup):
    user_id = State()
    days = State()


# ============================================================
# DATABASE
# ============================================================

CREATE_USERS = """
CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT UNIQUE NOT NULL,
    username TEXT,
    first_name TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    last_seen TIMESTAMP NOT NULL DEFAULT NOW(),

    subscription_until TIMESTAMP NULL,
    is_blocked BOOLEAN NOT NULL DEFAULT FALSE
);
"""


CREATE_REPORTS = """
CREATE TABLE IF NOT EXISTS reports (
    id BIGSERIAL PRIMARY KEY,

    telegram_user_id BIGINT NOT NULL,
    telegram_username TEXT,

    target_type TEXT NOT NULL,
    category TEXT NOT NULL,

    target TEXT NOT NULL,
    description TEXT NOT NULL,

    generated_text TEXT NOT NULL,

    status TEXT NOT NULL DEFAULT 'NEW',

    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);
"""


async def init_database():
    global db_pool

    db_pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=10,
    )

    async with db_pool.acquire() as conn:
        await conn.execute(CREATE_USERS)
        await conn.execute(CREATE_REPORTS)

    logger.info("PostgreSQL connected")


# ============================================================
# USER
# ============================================================

async def register_user(user):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (
                telegram_id,
                username,
                first_name
            )
            VALUES ($1, $2, $3)
            ON CONFLICT (telegram_id)
            DO UPDATE SET
                username = EXCLUDED.username,
                first_name = EXCLUDED.first_name,
                last_seen = NOW()
            """,
            user.id,
            user.username,
            user.first_name,
        )


async def get_user(telegram_id):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT *
            FROM users
            WHERE telegram_id = $1
            """,
            telegram_id,
        )


async def has_subscription(telegram_id):
    user = await get_user(telegram_id)

    if not user:
        return False

    if user["is_blocked"]:
        return False

    subscription_until = user["subscription_until"]

    if not subscription_until:
        return False

    return subscription_until > datetime.now()


async def get_subscription_text(telegram_id):
    user = await get_user(telegram_id)

    if not user:
        return "❌ Подписка отсутствует."

    until = user["subscription_until"]

    if not until:
        return "❌ Подписка отсутствует."

    now = datetime.now()

    if until <= now:
        return "❌ Подписка истекла."

    remaining = until - now
    days = remaining.days
    hours = remaining.seconds // 3600

    return (
        "💎 <b>Подписка активна</b>\n\n"
        f"📅 До: <code>{until.strftime('%d.%m.%Y %H:%M')}</code>\n"
        f"⏳ Осталось: <b>{days} дн. {hours} ч.</b>"
    )


# ============================================================
# SUBSCRIPTION
# ============================================================

async def give_subscription(telegram_id, days):
    until = datetime.now() + timedelta(days=days)

    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE users
            SET subscription_until = $1
            WHERE telegram_id = $2
            """,
            until,
            telegram_id,
        )

    return until


async def remove_subscription(telegram_id):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE users
            SET subscription_until = NULL
            WHERE telegram_id = $1
            """,
            telegram_id,
        )


# ============================================================
# REPORT
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
    "copyright": "Авторские права",
    "prohibited": "Запрещённый контент",
    "other": "Другое",
}


STATUS_NAMES = {
    "NEW": "🆕 Новый",
    "WORK": "🔄 В работе",
    "DONE": "✅ Завершён",
    "REJECTED": "❌ Отклонён",
}


def generate_report_text(
    target_type,
    category,
    target,
    description,
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
        "Хочу сообщить о потенциальном "
        "нарушении правил Telegram.\n\n"
        f"Тип объекта: {type_name}\n"
        f"Объект: {target}\n"
        f"Категория: {category_name}\n\n"
        "Описание ситуации:\n"
        f"{description}\n\n"
        "Прошу проверить указанную информацию "
        "и принять соответствующие меры, если "
        "нарушение подтвердится.\n\n"
        "Спасибо."
    )


async def create_report(
    telegram_id,
    username,
    target_type,
    category,
    target,
    description,
    generated_text,
):
    async with db_pool.acquire() as conn:
        return await conn.fetchval(
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
            VALUES (
                $1, $2, $3, $4,
                $5, $6, $7, 'NEW'
            )
            RETURNING id
            """,
            telegram_id,
            username,
            target_type,
            category,
            target,
            description,
            generated_text,
        )


# ============================================================
# KEYBOARDS
# ============================================================

def main_keyboard():
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
                    text="💎 Моя подписка",
                    callback_data="subscription",
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
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎭 Выдача себя за другого",
                    callback_data="category:impersonation",
                )
            ],
            [
                InlineKeyboardButton(
                    text="💰 Мошенничество",
                    callback_data="category:fraud",
                )
            ],
            [
                InlineKeyboardButton(
                    text="©️ Авторские права",
                    callback_data="category:copyright",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔞 Запрещённый контент",
                    callback_data="category:prohibited",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⚠️ Другое",
                    callback_data="category:other",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data="cancel",
                )
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
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Заново",
                    callback_data="report:restart",
                ),
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data="cancel",
                ),
            ],
        ]
    )


def admin_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📊 Статистика",
                    callback_data="admin:stats",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📋 Все обращения",
                    callback_data="admin:reports",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🆕 Новые",
                    callback_data="admin:reports:NEW",
                ),
                InlineKeyboardButton(
                    text="🔄 В работе",
                    callback_data="admin:reports:WORK",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="✅ Завершённые",
                    callback_data="admin:reports:DONE",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="👥 Пользователи",
                    callback_data="admin:users",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💎 Выдать подписку",
                    callback_data="admin:subscription",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="❌ Снять подписку",
                    callback_data="admin:remove_sub",
                ),
            ],
        ]
    )


def back_admin_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ Админ-панель",
                    callback_data="admin:home",
                )
            ]
        ]
    )


def report_status_keyboard(report_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 В работу",
                    callback_data=f"status:{report_id}:WORK",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="✅ Завершить",
                    callback_data=f"status:{report_id}:DONE",
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data=f"status:{report_id}:REJECTED",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data="admin:reports",
                )
            ],
        ]
    )


# ============================================================
# ACCESS
# ============================================================

async def check_access(
    callback: CallbackQuery,
):
    if callback.from_user.id == ADMIN_ID:
        return True

    return await has_subscription(
        callback.from_user.id
    )


# ============================================================
# START
# ============================================================

@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()

    await register_user(
        message.from_user
    )

    if await has_subscription(
        message.from_user.id
    ):
        await message.answer(
            "<b>🔥 FENIX REPORT</b>\n\n"
            "💎 Подписка активна.\n\n"
            "Выберите действие:",
            reply_markup=main_keyboard(),
        )
        return

    await message.answer(
        "<b>🔥 FENIX REPORT</b>\n\n"
        "⛔ Доступ закрыт.\n\n"
        "Для использования системы "
        "необходима активная подписка.\n\n"
        "Выдать её может только администратор."
    )


# ============================================================
# SUBSCRIPTION
# ============================================================

@dp.callback_query(F.data == "subscription")
async def subscription(
    callback: CallbackQuery,
):
    if not await check_access(callback):
        await callback.answer(
            "⛔ Нет активной подписки",
            show_alert=True,
        )
        return

    await callback.message.edit_text(
        await get_subscription_text(
            callback.from_user.id
        ),
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
# CREATE REPORT - TYPE
# ============================================================

@dp.callback_query(F.data.startswith("type:"))
async def choose_type(
    callback: CallbackQuery,
    state: FSMContext,
):
    if not await check_access(callback):
        await callback.answer(
            "⛔ Необходима подписка",
            show_alert=True,
        )
        return

    target_type = callback.data.split(":")[1]

    await state.update_data(
        target_type=target_type
    )

    await state.set_state(
        ReportForm.category
    )

    await callback.message.edit_text(
        "<b>⚠️ Выберите категорию</b>",
        reply_markup=categories_keyboard(),
    )

    await callback.answer()


# ============================================================
# CATEGORY
# ============================================================

@dp.callback_query(F.data.startswith("category:"))
async def choose_category(
    callback: CallbackQuery,
    state: FSMContext,
):
    if not await check_access(callback):
        await callback.answer(
            "⛔ Необходима подписка",
            show_alert=True,
        )
        return

    category = callback.data.split(":")[1]

    await state.update_data(
        category=category
    )

    await state.set_state(
        ReportForm.target
    )

    await callback.message.edit_text(
        "<b>🔗 Отправьте объект</b>\n\n"
        "Можно отправить:\n\n"
        "• @username\n"
        "• https://t.me/username\n"
        "• ссылку на сообщение",
    )

    await callback.answer()


# ============================================================
# TARGET
# ============================================================

@dp.message(ReportForm.target)
async def target(
    message: Message,
    state: FSMContext,
):
    if not await has_subscription(
        message.from_user.id
    ):
        await state.clear()

        await message.answer(
            "⛔ Ваша подписка отсутствует "
            "или истекла."
        )
        return

    value = (message.text or "").strip()

    if len(value) < 2:
        await message.answer(
            "⚠️ Укажите @username или ссылку."
        )
        return

    if len(value) > 500:
        await message.answer(
            "⚠️ Слишком длинное значение."
        )
        return

    await state.update_data(
        target=value
    )

    await state.set_state(
        ReportForm.description
    )

    await message.answer(
        "<b>📝 Опишите ситуацию</b>\n\n"
        "Напишите реальные обстоятельства "
        "нарушения."
    )


# ============================================================
# DESCRIPTION
# ============================================================

@dp.message(ReportForm.description)
async def description(
    message: Message,
    state: FSMContext,
):
    if not await has_subscription(
        message.from_user.id
    ):
        await state.clear()

        await message.answer(
            "⛔ Ваша подписка истекла."
        )
        return

    value = (message.text or "").strip()

    if len(value) < 10:
        await message.answer(
            "⚠️ Нужно добавить больше информации."
        )
        return

    if len(value) > 4000:
        await message.answer(
            "⚠️ Максимум 4000 символов."
        )
        return

    await state.update_data(
        description=value
    )

    data = await state.get_data()

    generated = generate_report_text(
        data["target_type"],
        data["category"],
        data["target"],
        data["description"],
    )

    await state.update_data(
        generated_text=generated
    )

    preview = (
        "<b>📋 ПРЕДПРОСМОТР</b>\n\n"
        f"<b>Тип:</b> "
        f"{html.escape(TYPE_NAMES[data['target_type']])}\n"
        f"<b>Категория:</b> "
        f"{html.escape(CATEGORY_NAMES[data['category']])}\n"
        f"<b>Объект:</b> "
        f"{html.escape(data['target'])}\n\n"
        "<b>Сформированный текст:</b>\n\n"
        f"{html.escape(generated)}"
    )

    await message.answer(
        preview,
        reply_markup=preview_keyboard(),
    )


# ============================================================
# CONFIRM REPORT
# ============================================================

@dp.callback_query(F.data == "report:confirm")
async def confirm_report(
    callback: CallbackQuery,
    state: FSMContext,
):
    if not await check_access(callback):
        await callback.answer(
            "⛔ Подписка отсутствует",
            show_alert=True,
        )
        await state.clear()
        return

    data = await state.get_data()

    if not data:
        await callback.answer(
            "Сессия устарела.",
            show_alert=True,
        )
        return

    report_id = await create_report(
        telegram_id=callback.from_user.id,
        username=callback.from_user.username,
        target_type=data["target_type"],
        category=data["category"],
        target=data["target"],
        description=data["description"],
        generated_text=data["generated_text"],
    )

    await state.clear()

    await callback.message.edit_text(
        "<b>✅ Обращение создано</b>\n\n"
        f"🆔 ID: <code>#{report_id}</code>\n"
        "📌 Статус: <b>NEW</b>\n\n"
        "Обращение сохранено в PostgreSQL.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🏠 Главное меню",
                        callback_data="back:main",
                    )
                ]
            ]
        ),
    )

    await callback.answer("Готово!")


# ============================================================
# MY REPORTS
# ============================================================

@dp.callback_query(F.data == "my_reports")
async def my_reports(
    callback: CallbackQuery,
):
    if not await check_access(callback):
        await callback.answer(
            "⛔ Необходима подписка",
            show_alert=True,
        )
        return

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
            ORDER BY id DESC
            LIMIT 20
            """,
            callback.from_user.id,
        )

    if not rows:
        text = (
            "<b>📋 Мои обращения</b>\n\n"
            "Обращений пока нет."
        )
    else:
        text = "<b>📋 Мои обращения</b>\n\n"

        for row in rows:
            status = STATUS_NAMES.get(
                row["status"],
                row["status"],
            )

            text += (
                f"🆔 <code>#{row['id']}</code>\n"
                f"🎯 {TYPE_NAMES.get(row['target_type'])}\n"
                f"⚠️ {CATEGORY_NAMES.get(row['category'])}\n"
                f"🔗 {html.escape(row['target'])}\n"
                f"📌 {status}\n\n"
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
# ADMIN CHECK
# ============================================================

def is_admin(user_id):
    return user_id == ADMIN_ID


async def admin_only(callback):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "⛔ Доступ запрещён.",
            show_alert=True,
        )
        return False

    return True


# ============================================================
# /ADMIN
# ============================================================

@dp.message(Command("admin"))
async def admin_command(
    message: Message,
):
    await register_user(
        message.from_user
    )

    if not is_admin(
        message.from_user.id
    ):
        await message.answer(
            "⛔ У вас нет доступа к админ-панели."
        )
        return

    await message.answer(
        "<b>👑 FENIX ADMIN</b>\n\n"
        "Панель управления:",
        reply_markup=admin_keyboard(),
    )


# ============================================================
# ADMIN HOME
# ============================================================

@dp.callback_query(F.data == "admin:home")
async def admin_home(
    callback: CallbackQuery,
):
    if not await admin_only(callback):
        return

    await callback.message.edit_text(
        "<b>👑 FENIX ADMIN</b>\n\n"
        "Панель управления:",
        reply_markup=admin_keyboard(),
    )

    await callback.answer()


# ============================================================
# ADMIN STATS
# ============================================================

@dp.callback_query(F.data == "admin:stats")
async def admin_stats(
    callback: CallbackQuery,
):
    if not await admin_only(callback):
        return

    async with db_pool.acquire() as conn:

        users = await conn.fetchval(
            "SELECT COUNT(*) FROM users"
        )

        active_subs = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM users
            WHERE subscription_until > NOW()
            """
        )

        reports = await conn.fetchval(
            "SELECT COUNT(*) FROM reports"
        )

        new_reports = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM reports
            WHERE status = 'NEW'
            """
        )

        work_reports = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM reports
            WHERE status = 'WORK'
            """
        )

        done_reports = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM reports
            WHERE status = 'DONE'
            """
        )

    text = (
        "<b>📊 FENIX STATISTICS</b>\n\n"
        f"👥 Пользователей: <b>{users}</b>\n"
        f"💎 Активных подписок: <b>{active_subs}</b>\n\n"
        f"📋 Всего обращений: <b>{reports}</b>\n"
        f"🆕 Новых: <b>{new_reports}</b>\n"
        f"🔄 В работе: <b>{work_reports}</b>\n"
        f"✅ Завершено: <b>{done_reports}</b>"
    )

    await callback.message.edit_text(
        text,
        reply_markup=back_admin_keyboard(),
    )

    await callback.answer()


# ============================================================
# ADMIN USERS
# ============================================================

@dp.callback_query(F.data == "admin:users")
async def admin_users(
    callback: CallbackQuery,
):
    if not await admin_only(callback):
        return

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                telegram_id,
                username,
                subscription_until,
                created_at
            FROM users
            ORDER BY created_at DESC
            LIMIT 20
            """
        )

    text = "<b>👥 ПОСЛЕДНИЕ ПОЛЬЗОВАТЕЛИ</b>\n\n"

    if not rows:
        text += "Пользователей нет."
    else:
        for row in rows:
            username = (
                f"@{row['username']}"
                if row["username"]
                else "без username"
            )

            active = (
                "💎"
                if row["subscription_until"]
                and row["subscription_until"] > datetime.now()
                else "⛔"
            )

            text += (
                f"{active} <code>{row['telegram_id']}</code> "
                f"{html.escape(username)}\n"
            )

    await callback.message.edit_text(
        text,
        reply_markup=back_admin_keyboard(),
    )

    await callback.answer()


# ============================================================
# ADMIN REPORTS
# ============================================================

async def show_admin_reports(
    callback: CallbackQuery,
    status=None,
):
    if not await admin_only(callback):
        return

    async with db_pool.acquire() as conn:

        if status:
            rows = await conn.fetch(
                """
                SELECT
                    id,
                    telegram_user_id,
                    telegram_username,
                    target_type,
                    category,
                    target,
                    status,
                    created_at
                FROM reports
                WHERE status = $1
                ORDER BY id DESC
                LIMIT 20
                """,
                status,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT
                    id,
                    telegram_user_id,
                    telegram_username,
                    target_type,
                    category,
                    target,
                    status,
                    created_at
                FROM reports
                ORDER BY id DESC
                LIMIT 20
                """
            )

    title = (
        STATUS_NAMES.get(status, "📋 Все обращения")
        if status
        else "📋 Все обращения"
    )

    text = f"<b>{title}</b>\n\n"

    if not rows:
        text += "Обращений нет."

    for row in rows:
        username = (
            f"@{row['telegram_username']}"
            if row["telegram_username"]
            else str(row["telegram_user_id"])
        )

        text += (
            f"🆔 <code>#{row['id']}</code>\n"
            f"👤 {html.escape(username)}\n"
            f"🎯 {TYPE_NAMES.get(row['target_type'])}\n"
            f"⚠️ {CATEGORY_NAMES.get(row['category'])}\n"
            f"🔗 {html.escape(row['target'])}\n"
            f"📌 {STATUS_NAMES.get(row['status'])}\n\n"
        )

    keyboard = []

    for row in rows[:10]:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=f"👁 #{row['id']}",
                    callback_data=f"admin:report:{row['id']}",
                )
            ]
        )

    keyboard.append(
        [
            InlineKeyboardButton(
                text="⬅️ Админ-панель",
                callback_data="admin:home",
            )
        ]
    )

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=keyboard
        ),
    )

    await callback.answer()


@dp.callback_query(F.data == "admin:reports")
async def admin_reports(
    callback: CallbackQuery,
):
    await show_admin_reports(callback)


@dp.callback_query(F.data.startswith("admin:reports:"))
async def admin_reports_status(
    callback: CallbackQuery,
):
    status = callback.data.split(":")[-1]

    await show_admin_reports(
        callback,
        status,
    )


# ============================================================
# ADMIN REPORT DETAILS
# ============================================================

@dp.callback_query(F.data.startswith("admin:report:"))
async def admin_report_details(
    callback: CallbackQuery,
):
    if not await admin_only(callback):
        return

    report_id = int(
        callback.data.split(":")[-1]
    )

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT *
            FROM reports
            WHERE id = $1
            """,
            report_id,
        )

    if not row:
        await callback.answer(
            "Обращение не найдено.",
            show_alert=True,
        )
        return

    username = (
        f"@{row['telegram_username']}"
        if row["telegram_username"]
        else str(row["telegram_user_id"])
    )

    text = (
        f"<b>📋 ОБРАЩЕНИЕ #{row['id']}</b>\n\n"
        f"👤 Пользователь: "
        f"{html.escape(username)}\n"
        f"🆔 ID: <code>{row['telegram_user_id']}</code>\n"
        f"🎯 Тип: {TYPE_NAMES.get(row['target_type'])}\n"
        f"⚠️ Категория: "
        f"{CATEGORY_NAMES.get(row['category'])}\n"
        f"🔗 Объект: "
        f"{html.escape(row['target'])}\n\n"
        f"<b>📝 Описание:</b>\n"
        f"{html.escape(row['description'])}\n\n"
        f"<b>📄 Текст:</b>\n"
        f"{html.escape(row['generated_text'])}\n\n"
        f"📌 Статус: "
        f"<b>{STATUS_NAMES.get(row['status'])}</b>"
    )

    await callback.message.edit_text(
        text,
        reply_markup=report_status_keyboard(
            report_id
        ),
    )

    await callback.answer()


# ============================================================
# ADMIN CHANGE STATUS
# ============================================================

@dp.callback_query(F.data.startswith("status:"))
async def change_status(
    callback: CallbackQuery,
):
    if not await admin_only(callback):
        return

    _, report_id, status = (
        callback.data.split(":")
    )

    report_id = int(report_id)

    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE reports
            SET
                status = $1,
                updated_at = NOW()
            WHERE id = $2
            """,
            status,
            report_id,
        )

    await callback.answer(
        f"Статус изменён: {status}"
    )

    fake_callback = callback

    await admin_report_details(
        fake_callback
    )


# ============================================================
# ADMIN GIVE SUBSCRIPTION
# ============================================================

@dp.callback_query(F.data == "admin:subscription")
async def admin_subscription(
    callback: CallbackQuery,
    state: FSMContext,
):
    if not await admin_only(callback):
        return

    await state.clear()

    await state.set_state(
        SubscriptionForm.user_id
    )

    await callback.message.edit_text(
        "<b>💎 Выдача подписки</b>\n\n"
        "Отправьте Telegram ID пользователя."
    )

    await callback.answer()


@dp.message(SubscriptionForm.user_id)
async def subscription_user_id(
    message: Message,
    state: FSMContext,
):
    if message.from_user.id != ADMIN_ID:
        return

    value = (message.text or "").strip()

    try:
        user_id = int(value)
    except ValueError:
        await message.answer(
            "⚠️ Telegram ID должен быть числом."
        )
        return

    user = await get_user(user_id)

    if not user:
        await message.answer(
            "❌ Пользователь ещё не запускал бота."
        )
        await state.clear()
        return

    await state.update_data(
        target_user_id=user_id
    )

    await state.set_state(
        SubscriptionForm.days
    )

    await message.answer(
        "<b>📅 Срок подписки</b>\n\n"
        "Введите количество дней.\n\n"
        "Например:\n"
        "<code>7</code>\n"
        "<code>30</code>\n"
        "<code>365</code>"
    )


@dp.message(SubscriptionForm.days)
async def subscription_days(
    message: Message,
    state: FSMContext,
):
    if message.from_user.id != ADMIN_ID:
        return

    try:
        days = int(
            (message.text or "").strip()
        )
    except ValueError:
        await message.answer(
            "⚠️ Введите количество дней числом."
        )
        return

    if days <= 0 or days > 3650:
        await message.answer(
            "⚠️ Срок должен быть от 1 до 3650 дней."
        )
        return

    data = await state.get_data()

    user_id = data["target_user_id"]

    until = await give_subscription(
        user_id,
        days,
    )

    await state.clear()

    await message.answer(
        "<b>✅ Подписка выдана</b>\n\n"
        f"👤 ID: <code>{user_id}</code>\n"
        f"📅 Дней: <b>{days}</b>\n"
        f"⏳ До: <b>{until.strftime('%d.%m.%Y %H:%M')}</b>",
        reply_markup=admin_keyboard(),
    )


# ============================================================
# ADMIN REMOVE SUBSCRIPTION
# ============================================================

@dp.callback_query(F.data == "admin:remove_sub")
async def admin_remove_subscription(
    callback: CallbackQuery,
    state: FSMContext,
):
    if not await admin_only(callback):
        return

    await state.set_state(
        SubscriptionForm.user_id
    )

    await state.update_data(
        remove_mode=True
    )

    await callback.message.edit_text(
        "<b>❌ Снять подписку</b>\n\n"
        "Отправьте Telegram ID пользователя."
    )

    await callback.answer()


# ============================================================
# SPECIAL USER ID HANDLER FOR REMOVE
# ============================================================

@dp.message(SubscriptionForm.user_id)
async def subscription_user_handler(
    message: Message,
    state: FSMContext,
):
    if message.from_user.id != ADMIN_ID:
        return

    data = await state.get_data()

    if not data.get("remove_mode"):
        return

    try:
        user_id = int(
            (message.text or "").strip()
        )
    except ValueError:
        await message.answer(
            "⚠️ Некорректный Telegram ID."
        )
        return

    user = await get_user(user_id)

    if not user:
        await message.answer(
            "❌ Пользователь не найден."
        )
        await state.clear()
        return

    await remove_subscription(user_id)

    await state.clear()

    await message.answer(
        "<b>✅ Подписка снята</b>\n\n"
        f"👤 Пользователь: <code>{user_id}</code>",
        reply_markup=admin_keyboard(),
    )


# ============================================================
# CANCEL / BACK
# ============================================================

@dp.callback_query(F.data == "cancel")
async def cancel(
    callback: CallbackQuery,
    state: FSMContext,
):
    await state.clear()

    await callback.message.edit_text(
        "<b>❌ Отменено</b>\n\n"
        "Главное меню:",
        reply_markup=main_keyboard(),
    )

    await callback.answer()


@dp.callback_query(F.data == "back:main")
async def back_main(
    callback: CallbackQuery,
    state: FSMContext,
):
    await state.clear()

    await callback.message.edit_text(
        "<b>🔥 FENIX REPORT</b>\n\n"
        "Выберите действие:",
        reply_markup=main_keyboard(),
    )

    await callback.answer()


# ============================================================
# FALLBACK
# ============================================================

@dp.message()
async def fallback(message: Message):
    await register_user(
        message.from_user
    )

    if message.from_user.id == ADMIN_ID:
        await message.answer(
            "<b>👑 Вы администратор.</b>\n\n"
            "Используйте /admin"
        )
        return

    if not await has_subscription(
        message.from_user.id
    ):
        await message.answer(
            "<b>⛔ Доступ закрыт</b>\n\n"
            "Для использования Fenix Report "
            "необходима активная подписка.\n\n"
            "Обратитесь к администратору."
        )
        return

    await message.answer(
        "<b>🔥 FENIX REPORT</b>\n\n"
        "Выберите действие:",
        reply_markup=main_keyboard(),
    )


# ============================================================
# MAIN
# ============================================================

async def main():
    logger.info("Starting Fenix...")

    await init_database()

    me = await bot.get_me()

    logger.info(
        "Bot started: @%s",
        me.username,
    )

    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped")