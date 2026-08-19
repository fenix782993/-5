import os
import asyncio
import logging
import threading

from datetime import datetime, timedelta, timezone
from html import escape

import asyncpg

from flask import Flask

from aiogram import Bot, Dispatcher, F
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
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("fenix")


# ============================================================
# FLASK / RENDER WEB SERVICE
# ============================================================

app = Flask(__name__)


@app.get("/")
def index():
    return "🔥 FENIX REPORT is online", 200


@app.get("/health")
def health():
    telegram_status = (
        "running"
        if _bot_thread is not None and _bot_thread.is_alive()
        else "starting"
    )

    return {
        "status": "ok",
        "service": "fenix-report",
        "telegram_bot": telegram_status,
    }, 200


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

ADMIN_ID_RAW = os.getenv("ADMIN_ID", "0").strip()

try:
    ADMIN_ID = int(ADMIN_ID_RAW)
except ValueError:
    ADMIN_ID = 0


if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан в Environment Variables")


if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL не задан в Environment Variables")


# Render/PostgreSQL sometimes gives postgres://
# asyncpg prefers postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace(
        "postgres://",
        "postgresql://",
        1
    )

# Remove accidental quotes from Render variable
DATABASE_URL = DATABASE_URL.strip("\"'")


# ============================================================
# TELEGRAM
# ============================================================

bot = Bot(
    token=BOT_TOKEN
)

dp = Dispatcher(
    storage=MemoryStorage()
)


# PostgreSQL pool
db: asyncpg.Pool | None = None


# ============================================================
# BOT THREAD VARIABLES
# ============================================================

_bot_thread = None
_bot_thread_lock = threading.Lock()


# ============================================================
# CATEGORIES
# ============================================================

CATEGORIES = {
    "spam": "📨 Спам",
    "phishing": "🎣 Фишинг",
    "fraud": "💳 Мошенничество",
    "harassment": "🚫 Домогательство / преследование",
    "doxxing": "🔐 Раскрытие персональных данных",
    "copyright": "©️ Авторские права",
    "privacy": "🛡 Нарушение приватности",
    "illegal_content": "⚠️ Запрещённый контент",
    "channel": "📢 Канал",
    "group": "👥 Группа",
    "other": "📋 Другое",
}


# ============================================================
# FSM
# ============================================================

class ComplaintForm(StatesGroup):
    category = State()
    target = State()
    details = State()
    preview = State()


class AdminSubscription(StatesGroup):
    user_id = State()
    duration = State()


class AdminRemoveSubscription(StatesGroup):
    user_id = State()


class AdminSearch(StatesGroup):
    user_id = State()


class AdminBroadcast(StatesGroup):
    text = State()


# ============================================================
# DATABASE
# ============================================================

async def init_db():

    global db

    if db is not None:
        return

    logger.info("Connecting to PostgreSQL...")

    db = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=5,
        command_timeout=30,
    )

    async with db.acquire() as conn:

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                telegram_id BIGINT PRIMARY KEY
                    REFERENCES users(telegram_id)
                    ON DELETE CASCADE,

                active BOOLEAN NOT NULL DEFAULT FALSE,

                expires_at TIMESTAMPTZ,

                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS complaints (
                id BIGSERIAL PRIMARY KEY,

                telegram_id BIGINT NOT NULL
                    REFERENCES users(telegram_id)
                    ON DELETE CASCADE,

                category TEXT NOT NULL,

                target TEXT NOT NULL,

                details TEXT NOT NULL,

                generated_text TEXT NOT NULL,

                status TEXT NOT NULL DEFAULT 'SAVED',

                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_users_last_seen
            ON users(last_seen)
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_complaints_user
            ON complaints(telegram_id)
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_complaints_status
            ON complaints(status)
        """)

    logger.info("PostgreSQL initialized successfully")


# ============================================================
# DATABASE HELPERS
# ============================================================

async def upsert_user(message: Message):

    if db is None:
        await init_db()

    if message.from_user is None:
        return

    user = message.from_user

    await db.execute("""
        INSERT INTO users (
            telegram_id,
            username,
            first_name,
            last_seen
        )
        VALUES ($1, $2, $3, NOW())

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

    await db.execute("""
        INSERT INTO subscriptions (
            telegram_id,
            active,
            expires_at
        )
        VALUES ($1, FALSE, NULL)

        ON CONFLICT (telegram_id)
        DO NOTHING
    """,
        user.id
    )


async def ensure_user(user_id: int):

    if db is None:
        await init_db()

    await db.execute("""
        INSERT INTO users (
            telegram_id
        )
        VALUES ($1)

        ON CONFLICT (telegram_id)
        DO NOTHING
    """,
        user_id
    )

    await db.execute("""
        INSERT INTO subscriptions (
            telegram_id,
            active,
            expires_at
        )
        VALUES ($1, FALSE, NULL)

        ON CONFLICT (telegram_id)
        DO NOTHING
    """,
        user_id
    )


async def has_subscription(user_id: int) -> bool:

    # ADMIN always has access
    if user_id == ADMIN_ID:
        return True

    if db is None:
        await init_db()

    row = await db.fetchrow("""
        SELECT
            active,
            expires_at
        FROM subscriptions
        WHERE telegram_id = $1
    """,
        user_id
    )

    if not row:
        return False

    if not row["active"]:
        return False

    expires_at = row["expires_at"]

    # Permanent subscription
    if expires_at is None:
        return True

    # Expired subscription
    if expires_at <= datetime.now(timezone.utc):

        await db.execute("""
            UPDATE subscriptions
            SET
                active = FALSE,
                updated_at = NOW()
            WHERE telegram_id = $1
        """,
            user_id
        )

        return False

    return True


# ============================================================
# KEYBOARDS
# ============================================================

def main_menu():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📝 Создать обращение",
                    callback_data="complaint_create"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📋 Мои обращения",
                    callback_data="my_complaints"
                ),
                InlineKeyboardButton(
                    text="👤 Профиль",
                    callback_data="profile"
                )
            ],
            [
                InlineKeyboardButton(
                    text="ℹ️ Помощь",
                    callback_data="help"
                )
            ],
        ]
    )


def subscription_menu():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Проверить подписку",
                    callback_data="check_subscription"
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data="back_main"
                )
            ]
        ]
    )


def category_menu():

    buttons = []

    items = list(CATEGORIES.items())

    for i in range(0, len(items), 2):

        row = []

        for key, title in items[i:i + 2]:

            row.append(
                InlineKeyboardButton(
                    text=title,
                    callback_data=f"category:{key}"
                )
            )

        buttons.append(row)

    buttons.append([
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data="cancel"
        )
    ])

    return InlineKeyboardMarkup(
        inline_keyboard=buttons
    )


def cancel_menu():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data="cancel"
                )
            ]
        ]
    )


def preview_menu():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Сохранить",
                    callback_data="complaint_save"
                )
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Изменить",
                    callback_data="complaint_edit"
                ),
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data="cancel"
                )
            ]
        ]
    )


def admin_menu():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📊 Статистика",
                    callback_data="admin_stats"
                ),
                InlineKeyboardButton(
                    text="👥 Пользователи",
                    callback_data="admin_users"
                )
            ],
            [
                InlineKeyboardButton(
                    text="➕ Выдать подписку",
                    callback_data="admin_give_sub"
                ),
                InlineKeyboardButton(
                    text="➖ Забрать подписку",
                    callback_data="admin_remove_sub"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔎 Найти пользователя",
                    callback_data="admin_search"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📋 Обращения",
                    callback_data="admin_complaints"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📢 Рассылка",
                    callback_data="admin_broadcast"
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Главное меню",
                    callback_data="back_main"
                )
            ]
        ]
    )


# ============================================================
# TEXT GENERATOR
# ============================================================

def generate_complaint(
    category: str,
    target: str,
    details: str
) -> str:

    category_name = CATEGORIES.get(
        category,
        "📋 Другое"
    )

    safe_target = escape(target)
    safe_details = escape(details)

    return (
        "Здравствуйте.\n\n"
        f"Хочу сообщить о возможном нарушении "
        f"категории: {category_name}.\n\n"
        f"Объект обращения:\n"
        f"{safe_target}\n\n"
        f"Описание ситуации:\n"
        f"{safe_details}\n\n"
        "Прошу проверить указанную информацию "
        "и принять соответствующие меры, "
        "если нарушение подтвердится.\n\n"
        "Спасибо."
    )


# ============================================================
# START
# ============================================================

@dp.message(CommandStart())
async def start(
    message: Message,
    state: FSMContext
):

    logger.info(
        "START from user %s",
        message.from_user.id
    )

    await state.clear()

    await upsert_user(message)

    user_id = message.from_user.id

    if not await has_subscription(user_id):

        await message.answer(
            "🔥 <b>FENIX REPORT</b>\n\n"
            "🔐 <b>Доступ закрыт.</b>\n\n"
            "Для использования системы необходима "
            "активная подписка.\n\n"
            "Администратор должен выдать вам доступ.",
            parse_mode="HTML",
            reply_markup=subscription_menu()
        )

        return

    await message.answer(
        "🔥 <b>FENIX REPORT</b>\n\n"
        "Добро пожаловать.\n\n"
        "Выберите нужный раздел:",
        parse_mode="HTML",
        reply_markup=main_menu()
    )


# ============================================================
# PROFILE
# ============================================================

@dp.callback_query(F.data == "profile")
async def profile(callback: CallbackQuery):

    user_id = callback.from_user.id

    if db is None:
        await init_db()

    row = await db.fetchrow("""
        SELECT
            active,
            expires_at
        FROM subscriptions
        WHERE telegram_id = $1
    """,
        user_id
    )

    count = await db.fetchval("""
        SELECT COUNT(*)
        FROM complaints
        WHERE telegram_id = $1
    """,
        user_id
    )

    active = bool(row and row["active"])

    if row and row["expires_at"]:
        expires = row["expires_at"].strftime(
            "%d.%m.%Y %H:%M"
        )
    else:
        expires = "∞"

    username = (
        f"@{escape(callback.from_user.username)}"
        if callback.from_user.username
        else "нет"
    )

    text = (
        "👤 <b>ПРОФИЛЬ</b>\n\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"👤 Username: {username}\n\n"
        f"🔐 Подписка: "
        f"{'✅ АКТИВНА' if active or user_id == ADMIN_ID else '❌ НЕТ'}\n"
        f"📅 До: {expires}\n"
        f"📋 Обращений: {count}"
    )

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=main_menu()
    )

    await callback.answer()


# ============================================================
# CHECK SUBSCRIPTION
# ============================================================

@dp.callback_query(F.data == "check_subscription")
async def check_subscription(
    callback: CallbackQuery
):

    active = await has_subscription(
        callback.from_user.id
    )

    if active:

        await callback.message.edit_text(
            "✅ <b>Подписка активна.</b>\n\n"
            "Доступ к FENIX REPORT открыт.",
            parse_mode="HTML",
            reply_markup=main_menu()
        )

        await callback.answer(
            "Подписка активна."
        )

    else:

        await callback.answer(
            "❌ Активной подписки нет.",
            show_alert=True
        )


# ============================================================
# CREATE COMPLAINT
# ============================================================

@dp.callback_query(F.data == "complaint_create")
async def complaint_create(
    callback: CallbackQuery,
    state: FSMContext
):

    if not await has_subscription(
        callback.from_user.id
    ):

        await callback.answer(
            "🔐 Нужна активная подписка.",
            show_alert=True
        )

        return

    await state.clear()

    await state.set_state(
        ComplaintForm.category
    )

    await callback.message.edit_text(
        "📝 <b>СОЗДАНИЕ ОБРАЩЕНИЯ</b>\n\n"
        "Шаг 1/3\n\n"
        "Выберите категорию нарушения:",
        parse_mode="HTML",
        reply_markup=category_menu()
    )

    await callback.answer()


# ============================================================
# CATEGORY
# ============================================================

@dp.callback_query(
    F.data.startswith("category:")
)
async def select_category(
    callback: CallbackQuery,
    state: FSMContext
):

    category = callback.data.split(
        ":",
        1
    )[1]

    if category not in CATEGORIES:

        await callback.answer(
            "Неизвестная категория.",
            show_alert=True
        )

        return

    await state.update_data(
        category=category
    )

    await state.set_state(
        ComplaintForm.target
    )

    await callback.message.edit_text(
        "🎯 <b>ШАГ 2/3</b>\n\n"
        "Отправьте:\n\n"
        "• @username\n"
        "• ссылку на профиль\n"
        "• ссылку на канал\n"
        "• ссылку на группу\n"
        "• ссылку на конкретное сообщение\n\n"
        "Укажите только реальный объект обращения.",
        parse_mode="HTML",
        reply_markup=cancel_menu()
    )

    await callback.answer()


# ============================================================
# TARGET
# ============================================================

@dp.message(ComplaintForm.target)
async def complaint_target(
    message: Message,
    state: FSMContext
):

    target = (message.text or "").strip()

    if len(target) < 2:

        await message.answer(
            "⚠️ Укажите корректный username или ссылку."
        )

        return

    if len(target) > 500:

        await message.answer(
            "⚠️ Ссылка или username слишком длинные."
        )

        return

    await state.update_data(
        target=target
    )

    await state.set_state(
        ComplaintForm.details
    )

    await message.answer(
        "📝 <b>ШАГ 3/3</b>\n\n"
        "Опишите фактическую ситуацию.\n\n"
        "Не придумывайте сведения.\n\n"
        "Текст обращения будет сформирован "
        "автоматически.",
        parse_mode="HTML",
        reply_markup=cancel_menu()
    )


# ============================================================
# DETAILS
# ============================================================

@dp.message(ComplaintForm.details)
async def complaint_details(
    message: Message,
    state: FSMContext
):

    details = (message.text or "").strip()

    if len(details) < 10:

        await message.answer(
            "⚠️ Описание слишком короткое."
        )

        return

    if len(details) > 4000:

        await message.answer(
            "⚠️ Максимум 4000 символов."
        )

        return

    data = await state.get_data()

    category = data["category"]
    target = data["target"]

    generated = generate_complaint(
        category,
        target,
        details
    )

    await state.update_data(
        details=details,
        generated=generated
    )

    await state.set_state(
        ComplaintForm.preview
    )

    await message.answer(
        "👀 <b>ПРЕДПРОСМОТР</b>\n\n"
        f"{generated}\n\n"
        "Проверьте текст перед сохранением.",
        parse_mode="HTML",
        reply_markup=preview_menu()
    )


# ============================================================
# SAVE
# ============================================================

@dp.callback_query(F.data == "complaint_save")
async def complaint_save(
    callback: CallbackQuery,
    state: FSMContext
):

    if not await has_subscription(
        callback.from_user.id
    ):

        await state.clear()

        await callback.answer(
            "❌ Подписка закончилась.",
            show_alert=True
        )

        return

    data = await state.get_data()

    required = [
        "category",
        "target",
        "details",
        "generated"
    ]

    if not all(
        key in data
        for key in required
    ):

        await state.clear()

        await callback.answer(
            "❌ Данные обращения потеряны.",
            show_alert=True
        )

        return

    if db is None:
        await init_db()

    await db.execute("""
        INSERT INTO complaints (
            telegram_id,
            category,
            target,
            details,
            generated_text,
            status
        )
        VALUES (
            $1,
            $2,
            $3,
            $4,
            $5,
            'SAVED'
        )
    """,
        callback.from_user.id,
        data["category"],
        data["target"],
        data["details"],
        data["generated"]
    )

    await state.clear()

    await callback.message.edit_text(
        "✅ <b>ОБРАЩЕНИЕ СОХРАНЕНО</b>\n\n"
        "Оно добавлено в PostgreSQL.",
        parse_mode="HTML",
        reply_markup=main_menu()
    )

    await callback.answer(
        "Сохранено."
    )


# ============================================================
# EDIT
# ============================================================

@dp.callback_query(F.data == "complaint_edit")
async def complaint_edit(
    callback: CallbackQuery,
    state: FSMContext
):

    data = await state.get_data()

    if "category" not in data:

        await callback.answer(
            "Данные обращения отсутствуют.",
            show_alert=True
        )

        return

    await state.set_state(
        ComplaintForm.details
    )

    await callback.message.edit_text(
        "✏️ <b>ИЗМЕНЕНИЕ</b>\n\n"
        "Отправьте новое описание ситуации.",
        parse_mode="HTML",
        reply_markup=cancel_menu()
    )

    await callback.answer()


# ============================================================
# MY COMPLAINTS
# ============================================================

@dp.callback_query(F.data == "my_complaints")
async def my_complaints(
    callback: CallbackQuery
):

    if db is None:
        await init_db()

    rows = await db.fetch("""
        SELECT
            id,
            category,
            target,
            status,
            created_at
        FROM complaints
        WHERE telegram_id = $1
        ORDER BY id DESC
        LIMIT 15
    """,
        callback.from_user.id
    )

    if not rows:

        text = (
            "📋 <b>МОИ ОБРАЩЕНИЯ</b>\n\n"
            "У вас пока нет обращений."
        )

    else:

        lines = [
            "📋 <b>МОИ ОБРАЩЕНИЯ</b>\n"
        ]

        for row in rows:

            category = CATEGORIES.get(
                row["category"],
                row["category"]
            )

            target = escape(
                row["target"]
            )

            created = row[
                "created_at"
            ].strftime(
                "%d.%m.%Y %H:%M"
            )

            lines.append(
                f"🆔 <b>#{row['id']}</b>\n"
                f"📁 {category}\n"
                f"🎯 {target}\n"
                f"📌 {row['status']}\n"
                f"🕐 {created}\n"
            )

        text = "\n".join(lines)

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=main_menu()
    )

    await callback.answer()


# ============================================================
# HELP
# ============================================================

@dp.callback_query(F.data == "help")
async def help_menu(
    callback: CallbackQuery
):

    await callback.message.edit_text(
        "ℹ️ <b>FENIX REPORT — ПОМОЩЬ</b>\n\n"
        "📝 <b>Создание обращения</b>\n\n"
        "1. Выберите категорию.\n"
        "2. Укажите @username или ссылку.\n"
        "3. Опишите ситуацию.\n"
        "4. Бот сформирует текст.\n"
        "5. Проверьте предпросмотр.\n"
        "6. Сохраните обращение.\n\n"
        "📦 Обращения хранятся в PostgreSQL.",
        parse_mode="HTML",
        reply_markup=main_menu()
    )

    await callback.answer()


# ============================================================
# CANCEL
# ============================================================

@dp.callback_query(F.data == "cancel")
async def cancel(
    callback: CallbackQuery,
    state: FSMContext
):

    await state.clear()

    await callback.message.edit_text(
        "❌ <b>Операция отменена.</b>\n\n"
        "Главное меню:",
        parse_mode="HTML",
        reply_markup=main_menu()
    )

    await callback.answer()


# ============================================================
# BACK MAIN
# ============================================================

@dp.callback_query(F.data == "back_main")
async def back_main(
    callback: CallbackQuery,
    state: FSMContext
):

    await state.clear()

    await callback.message.edit_text(
        "🔥 <b>FENIX REPORT</b>\n\n"
        "Главное меню:",
        parse_mode="HTML",
        reply_markup=main_menu()
    )

    await callback.answer()


# ============================================================
# ADMIN HELPERS
# ============================================================

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


async def admin_required(
    callback: CallbackQuery
) -> bool:

    if not is_admin(
        callback.from_user.id
    ):

        await callback.answer(
            "⛔ Доступ запрещён.",
            show_alert=True
        )

        return False

    return True


# ============================================================
# ADMIN COMMAND
# ============================================================

@dp.message(Command("admin"))
async def admin_command(
    message: Message
):

    await upsert_user(message)

    if not is_admin(
        message.from_user.id
    ):

        await message.answer(
            "⛔ Доступ запрещён."
        )

        return

    await message.answer(
        "👑 <b>FENIX ADMIN</b>\n\n"
        "Панель администратора:",
        parse_mode="HTML",
        reply_markup=admin_menu()
    )


# ============================================================
# ADMIN STATS
# ============================================================

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(
    callback: CallbackQuery
):

    if not await admin_required(callback):
        return

    if db is None:
        await init_db()

    users = await db.fetchval("""
        SELECT COUNT(*)
        FROM users
    """)

    active_subs = await db.fetchval("""
        SELECT COUNT(*)
        FROM subscriptions
        WHERE active = TRUE
        AND (
            expires_at IS NULL
            OR expires_at > NOW()
        )
    """)

    complaints = await db.fetchval("""
        SELECT COUNT(*)
        FROM complaints
    """)

    saved = await db.fetchval("""
        SELECT COUNT(*)
        FROM complaints
        WHERE status = 'SAVED'
    """)

    await callback.message.edit_text(
        "📊 <b>СТАТИСТИКА FENIX</b>\n\n"
        f"👥 Пользователей: <b>{users}</b>\n"
        f"🔐 Активных подписок: <b>{active_subs}</b>\n"
        f"📋 Всего обращений: <b>{complaints}</b>\n"
        f"💾 Сохранено: <b>{saved}</b>",
        parse_mode="HTML",
        reply_markup=admin_menu()
    )

    await callback.answer()


# ============================================================
# ADMIN USERS
# ============================================================

@dp.callback_query(F.data == "admin_users")
async def admin_users(
    callback: CallbackQuery
):

    if not await admin_required(callback):
        return

    if db is None:
        await init_db()

    rows = await db.fetch("""
        SELECT
            u.telegram_id,
            u.username,
            s.active,
            s.expires_at
        FROM users u
        LEFT JOIN subscriptions s
            ON s.telegram_id = u.telegram_id
        ORDER BY u.last_seen DESC
        LIMIT 20
    """)

    if not rows:

        text = (
            "👥 <b>ПОЛЬЗОВАТЕЛИ</b>\n\n"
            "Пользователей пока нет."
        )

    else:

        lines = [
            "👥 <b>ПОСЛЕДНИЕ ПОЛЬЗОВАТЕЛИ</b>\n"
        ]

        for row in rows:

            active = row["active"]

            if active and row["expires_at"]:
                if row["expires_at"] <= datetime.now(timezone.utc):
                    active = False

            status = (
                "✅"
                if active
                else "❌"
            )

            username = (
                "@" + escape(row["username"])
                if row["username"]
                else "без username"
            )

            lines.append(
                f"{status} "
                f"<code>{row['telegram_id']}</code> "
                f"{username}"
            )

        text = "\n".join(lines)

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=admin_menu()
    )

    await callback.answer()


# ============================================================
# ADMIN GIVE SUB
# ============================================================

@dp.callback_query(F.data == "admin_give_sub")
async def admin_give_sub(
    callback: CallbackQuery,
    state: FSMContext
):

    if not await admin_required(callback):
        return

    await state.set_state(
        AdminSubscription.user_id
    )

    await callback.message.edit_text(
        "➕ <b>ВЫДАТЬ ПОДПИСКУ</b>\n\n"
        "Введите Telegram ID пользователя:",
        parse_mode="HTML",
        reply_markup=cancel_menu()
    )

    await callback.answer()


@dp.message(AdminSubscription.user_id)
async def admin_subscription_user(
    message: Message,
    state: FSMContext
):

    if not is_admin(message.from_user.id):
        return

    try:

        user_id = int(
            (message.text or "").strip()
        )

    except ValueError:

        await message.answer(
            "❌ ID должен быть числом."
        )

        return

    await ensure_user(user_id)

    await state.update_data(
        user_id=user_id
    )

    await state.set_state(
        AdminSubscription.duration
    )

    await message.answer(
        "⏱ <b>СРОК ПОДПИСКИ</b>\n\n"
        "1 — 1 день\n"
        "7 — 7 дней\n"
        "30 — 30 дней\n"
        "0 — навсегда\n\n"
        "Введите число:",
        parse_mode="HTML",
        reply_markup=cancel_menu()
    )


@dp.message(AdminSubscription.duration)
async def admin_subscription_duration(
    message: Message,
    state: FSMContext
):

    if not is_admin(message.from_user.id):
        return

    try:

        days = int(
            (message.text or "").strip()
        )

    except ValueError:

        await message.answer(
            "❌ Введите число."
        )

        return

    if days < 0:

        await message.answer(
            "❌ Неверный срок."
        )

        return

    data = await state.get_data()

    user_id = data["user_id"]

    await ensure_user(user_id)

    if days == 0:

        expires_at = None

    else:

        expires_at = (
            datetime.now(timezone.utc)
            + timedelta(days=days)
        )

    await db.execute("""
        INSERT INTO subscriptions (
            telegram_id,
            active,
            expires_at,
            updated_at
        )
        VALUES ($1, TRUE, $2, NOW())

        ON CONFLICT (telegram_id)
        DO UPDATE SET
            active = TRUE,
            expires_at = EXCLUDED.expires_at,
            updated_at = NOW()
    """,
        user_id,
        expires_at
    )

    await state.clear()

    expiration = (
        "навсегда"
        if expires_at is None
        else expires_at.strftime(
            "%d.%m.%Y %H:%M"
        )
    )

    await message.answer(
        "✅ <b>ПОДПИСКА ВЫДАНА</b>\n\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"📅 До: <b>{expiration}</b>",
        parse_mode="HTML",
        reply_markup=admin_menu()
    )

    try:

        await bot.send_message(
            user_id,
            "🔥 <b>FENIX REPORT</b>\n\n"
            "✅ Администратор выдал вам доступ.",
            parse_mode="HTML",
            reply_markup=main_menu()
        )

    except Exception as e:

        logger.warning(
            "Не удалось уведомить %s: %s",
            user_id,
            e
        )


# ============================================================
# ADMIN REMOVE SUB
# ============================================================

@dp.callback_query(F.data == "admin_remove_sub")
async def admin_remove_sub(
    callback: CallbackQuery,
    state: FSMContext
):

    if not await admin_required(callback):
        return

    await state.set_state(
        AdminRemoveSubscription.user_id
    )

    await callback.message.edit_text(
        "➖ <b>ЗАБРАТЬ ПОДПИСКУ</b>\n\n"
        "Введите Telegram ID:",
        parse_mode="HTML",
        reply_markup=cancel_menu()
    )

    await callback.answer()


@dp.message(AdminRemoveSubscription.user_id)
async def admin_remove_sub_user(
    message: Message,
    state: FSMContext
):

    if not is_admin(message.from_user.id):
        return

    try:

        user_id = int(
            (message.text or "").strip()
        )

    except ValueError:

        await message.answer(
            "❌ ID должен быть числом."
        )

        return

    if db is None:
        await init_db()

    await db.execute("""
        UPDATE subscriptions
        SET
            active = FALSE,
            expires_at = NOW(),
            updated_at = NOW()
        WHERE telegram_id = $1
    """,
        user_id
    )

    await state.clear()

    await message.answer(
        "✅ <b>ПОДПИСКА ОТКЛЮЧЕНА</b>\n\n"
        f"🆔 ID: <code>{user_id}</code>",
        parse_mode="HTML",
        reply_markup=admin_menu()
    )


# ============================================================
# ADMIN SEARCH
# ============================================================

@dp.callback_query(F.data == "admin_search")
async def admin_search(
    callback: CallbackQuery,
    state: FSMContext
):

    if not await admin_required(callback):
        return

    await state.set_state(
        AdminSearch.user_id
    )

    await callback.message.edit_text(
        "🔎 <b>ПОИСК ПОЛЬЗОВАТЕЛЯ</b>\n\n"
        "Введите Telegram ID:",
        parse_mode="HTML",
        reply_markup=cancel_menu()
    )

    await callback.answer()


@dp.message(AdminSearch.user_id)
async def admin_search_result(
    message: Message,
    state: FSMContext
):

    if not is_admin(message.from_user.id):
        return

    try:

        user_id = int(
            (message.text or "").strip()
        )

    except ValueError:

        await message.answer(
            "❌ ID должен быть числом."
        )

        return

    if db is None:
        await init_db()

    row = await db.fetchrow("""
        SELECT
            u.telegram_id,
            u.username,
            u.first_name,
            u.created_at,
            u.last_seen,
            s.active,
            s.expires_at
        FROM users u
        LEFT JOIN subscriptions s
            ON s.telegram_id = u.telegram_id
        WHERE u.telegram_id = $1
    """,
        user_id
    )

    if not row:

        await state.clear()

        await message.answer(
            "❌ Пользователь не найден.",
            reply_markup=admin_menu()
        )

        return

    complaints = await db.fetchval("""
        SELECT COUNT(*)
        FROM complaints
        WHERE telegram_id = $1
    """,
        user_id
    )

    username = (
        "@" + escape(row["username"])
        if row["username"]
        else "нет"
    )

    expires = (
        row["expires_at"].strftime(
            "%d.%m.%Y %H:%M"
        )
        if row["expires_at"]
        else "∞"
    )

    active = bool(row["active"])

    if active and row["expires_at"]:
        if row["expires_at"] <= datetime.now(timezone.utc):
            active = False

    status = (
        "✅ Активна"
        if active
        else "❌ Нет"
    )

    await state.clear()

    await message.answer(
        "🔎 <b>ПОЛЬЗОВАТЕЛЬ</b>\n\n"
        f"🆔 ID: <code>{row['telegram_id']}</code>\n"
        f"👤 Username: {username}\n"
        f"📛 Имя: "
        f"{escape(row['first_name'] or 'нет')}\n\n"
        f"🔐 Подписка: {status}\n"
        f"📅 До: {expires}\n"
        f"📋 Обращений: {complaints}\n\n"
        f"🕐 Последняя активность: "
        f"{row['last_seen'].strftime('%d.%m.%Y %H:%M')}",
        parse_mode="HTML",
        reply_markup=admin_menu()
    )


# ============================================================
# ADMIN COMPLAINTS
# ============================================================

@dp.callback_query(F.data == "admin_complaints")
async def admin_complaints(
    callback: CallbackQuery
):

    if not await admin_required(callback):
        return

    if db is None:
        await init_db()

    rows = await db.fetch("""
        SELECT
            id,
            telegram_id,
            category,
            target,
            status,
            created_at
        FROM complaints
        ORDER BY id DESC
        LIMIT 20
    """)

    if not rows:

        text = (
            "📋 <b>ОБРАЩЕНИЯ</b>\n\n"
            "Обращений пока нет."
        )

    else:

        lines = [
            "📋 <b>ПОСЛЕДНИЕ ОБРАЩЕНИЯ</b>\n"
        ]

        for row in rows:

            category = CATEGORIES.get(
                row["category"],
                row["category"]
            )

            target = escape(
                row["target"]
            )

            created = row[
                "created_at"
            ].strftime(
                "%d.%m.%Y %H:%M"
            )

            lines.append(
                f"🆔 #{row['id']}\n"
                f"👤 <code>{row['telegram_id']}</code>\n"
                f"📁 {category}\n"
                f"🎯 {target}\n"
                f"📌 {row['status']}\n"
                f"🕐 {created}\n"
            )

        text = "\n".join(lines)

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=admin_menu()
    )

    await callback.answer()


# ============================================================
# ADMIN BROADCAST
# ============================================================

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(
    callback: CallbackQuery,
    state: FSMContext
):

    if not await admin_required(callback):
        return

    await state.set_state(
        AdminBroadcast.text
    )

    await callback.message.edit_text(
        "📢 <b>РАССЫЛКА</b>\n\n"
        "Введите сообщение:",
        parse_mode="HTML",
        reply_markup=cancel_menu()
    )

    await callback.answer()


@dp.message(AdminBroadcast.text)
async def admin_broadcast_send(
    message: Message,
    state: FSMContext
):

    if not is_admin(message.from_user.id):
        return

    text = (message.text or "").strip()

    if not text:

        await message.answer(
            "❌ Текст пустой."
        )

        return

    if db is None:
        await init_db()

    users = await db.fetch("""
        SELECT telegram_id
        FROM users
    """)

    sent = 0
    failed = 0

    for row in users:

        try:

            await bot.send_message(
                row["telegram_id"],
                text
            )

            sent += 1

            # Telegram rate limit protection
            await asyncio.sleep(0.05)

        except Exception as e:

            failed += 1

            logger.warning(
                "Broadcast failed for %s: %s",
                row["telegram_id"],
                e
            )

    await state.clear()

    await message.answer(
        "📢 <b>РАССЫЛКА ЗАВЕРШЕНА</b>\n\n"
        f"✅ Отправлено: {sent}\n"
        f"❌ Ошибок: {failed}",
        parse_mode="HTML",
        reply_markup=admin_menu()
    )


# ============================================================
# FALLBACK
# ============================================================

@dp.message()
async def fallback(
    message: Message
):

    logger.info(
        "Message from %s: %s",
        message.from_user.id,
        message.text
    )

    await upsert_user(message)

    if not await has_subscription(
        message.from_user.id
    ):

        await message.answer(
            "🔐 Для доступа необходима "
            "активная подписка.",
            reply_markup=subscription_menu()
        )

        return

    await message.answer(
        "Используйте кнопки меню:",
        reply_markup=main_menu()
    )


# ============================================================
# TELEGRAM BOT RUNNER
# ============================================================

async def bot_runner():

    global db

    logger.info("====================================")
    logger.info("🔥 FENIX REPORT TELEGRAM STARTING")
    logger.info("====================================")

    try:

        while True:

            try:

                logger.info(
                    "Connecting to PostgreSQL..."
                )

                await init_db()

                logger.info(
                    "✅ PostgreSQL connected"
                )

                logger.info(
                    "🔥 Starting Telegram polling..."
                )

                await dp.start_polling(
                    bot,
                    allowed_updates=dp.resolve_used_update_types()
                )

                # If polling stops normally, wait before restart.
                logger.warning(
                    "Telegram polling stopped normally."
                )

            except asyncio.CancelledError:

                logger.info(
                    "Telegram bot polling cancelled."
                )

                raise

            except Exception:

                logger.exception(
                    "❌ Telegram bot crashed"
                )

                # Close broken PostgreSQL pool
                if db is not None:

                    try:
                        await db.close()

                    except Exception:
                        logger.exception(
                            "Error while closing PostgreSQL pool"
                        )

                    db = None

                logger.info(
                    "🔄 Retry Telegram bot in 10 seconds..."
                )

                await asyncio.sleep(10)

    except asyncio.CancelledError:

        logger.info(
            "Telegram bot runner cancelled"
        )

    except Exception:

        logger.exception(
            "❌ Fatal Telegram bot runner error"
        )

    finally:

        logger.info(
            "Telegram bot runner stopped"
        )

        if db is not None:

            try:
                await db.close()

            except Exception:
                logger.exception(
                    "Error while closing PostgreSQL"
                )

            db = None


# ============================================================
# START BOT THREAD
# ============================================================

def start_bot_thread():

    global _bot_thread

    with _bot_thread_lock:

        if (
            _bot_thread is not None
            and _bot_thread.is_alive()
        ):

            logger.info(
                "Telegram bot thread already running"
            )

            return

        def runner():

            try:

                asyncio.run(
                    bot_runner()
                )

            except Exception:

                logger.exception(
                    "Telegram bot thread crashed"
                )

        _bot_thread = threading.Thread(
            target=runner,
            name="fenix-telegram-bot",
            daemon=True
        )

        _bot_thread.start()

        logger.info(
            "🔥 Telegram bot background thread STARTED"
        )


# ============================================================
# IMPORTANT FOR GUNICORN
# ============================================================
#
# Render:
#
# gunicorn main:app --bind 0.0.0.0:$PORT --workers 1 --threads 4
#
# Gunicorn импортирует main.py.
#
# Поэтому start_bot_thread() вызывается при импорте.
# ============================================================

start_bot_thread()


# ============================================================
# LOCAL DEVELOPMENT
# ============================================================

if __name__ == "__main__":

    port = int(
        os.getenv(
            "PORT",
            "10000"
        )
    )

    logger.info(
        "🔥 Local Flask server on port %s",
        port
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False
    )
