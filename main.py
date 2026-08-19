import os
import logging
import asyncio
from datetime import datetime, timedelta, timezone

import asyncpg
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
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL не задан")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("fenix")


# ============================================================
# BOT
# ============================================================

bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

db: asyncpg.Pool | None = None


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


class AdminSearch(StatesGroup):
    user_id = State()


class AdminBroadcast(StatesGroup):
    text = State()


# ============================================================
# DATABASE
# ============================================================

async def init_db():
    global db

    db = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=5,
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
                telegram_id BIGINT PRIMARY KEY REFERENCES users(telegram_id)
                    ON DELETE CASCADE,
                active BOOLEAN NOT NULL DEFAULT FALSE,
                expires_at TIMESTAMPTZ
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS complaints (
                id BIGSERIAL PRIMARY KEY,
                telegram_id BIGINT NOT NULL REFERENCES users(telegram_id)
                    ON DELETE CASCADE,
                category TEXT NOT NULL,
                target TEXT NOT NULL,
                details TEXT NOT NULL,
                generated_text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'DRAFT',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_complaints_user
            ON complaints(telegram_id)
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_complaints_status
            ON complaints(status)
        """)


async def upsert_user(message: Message):
    if not db:
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
        ON CONFLICT (telegram_id) DO NOTHING
    """, user.id)


async def has_subscription(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True

    row = await db.fetchrow("""
        SELECT active, expires_at
        FROM subscriptions
        WHERE telegram_id = $1
    """, user_id)

    if not row:
        return False

    if not row["active"]:
        return False

    expires_at = row["expires_at"]

    if expires_at is None:
        return True

    return expires_at > datetime.now(timezone.utc)


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

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def preview_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Сохранить обращение",
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
            ],
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
                )
            ],
            [
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
            ],
        ]
    )


# ============================================================
# TEXT GENERATOR
# ============================================================

def generate_complaint(category: str, target: str, details: str) -> str:
    category_name = CATEGORIES.get(category, "Другое")

    return (
        "Здравствуйте.\n\n"
        f"Хочу сообщить о возможном нарушении категории: "
        f"{category_name}.\n\n"
        f"Объект обращения: {target}\n\n"
        f"Описание ситуации:\n{details}\n\n"
        "Прошу проверить указанную информацию и принять "
        "соответствующие меры, если нарушение подтвердится.\n\n"
        "Спасибо."
    )


# ============================================================
# START
# ============================================================

@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await upsert_user(message)

    subscribed = await has_subscription(message.from_user.id)

    if not subscribed and message.from_user.id != ADMIN_ID:
        await message.answer(
            "🔥 <b>FENIX REPORT</b>\n\n"
            "Доступ к системе обращений закрыт.\n\n"
            "🔐 Для использования сервиса необходима "
            "активная подписка.\n\n"
            "Обратитесь к администратору.",
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

    row = await db.fetchrow("""
        SELECT active, expires_at
        FROM subscriptions
        WHERE telegram_id = $1
    """, user_id)

    complaints = await db.fetchval("""
        SELECT COUNT(*)
        FROM complaints
        WHERE telegram_id = $1
    """, user_id)

    active = row and row["active"]

    if row and row["expires_at"]:
        expires = row["expires_at"].strftime("%d.%m.%Y %H:%M")
    else:
        expires = "∞"

    text = (
        "👤 <b>ПРОФИЛЬ</b>\n\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"👤 Username: @{callback.from_user.username or 'нет'}\n\n"
        f"🔐 Подписка: {'✅ АКТИВНА' if active else '❌ НЕТ'}\n"
        f"📅 До: {expires}\n"
        f"📋 Обращений: {complaints}"
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
async def check_subscription(callback: CallbackQuery):
    if await has_subscription(callback.from_user.id):
        await callback.message.edit_text(
            "✅ <b>Подписка активна.</b>\n\n"
            "Теперь вам доступен FENIX REPORT.",
            parse_mode="HTML",
            reply_markup=main_menu()
        )
    else:
        await callback.answer(
            "❌ Активной подписки нет.",
            show_alert=True
        )

    await callback.answer()


# ============================================================
# CREATE COMPLAINT
# ============================================================

@dp.callback_query(F.data == "complaint_create")
async def complaint_create(callback: CallbackQuery, state: FSMContext):

    if not await has_subscription(callback.from_user.id):
        await callback.answer(
            "🔐 Нужна активная подписка.",
            show_alert=True
        )
        return

    await state.clear()
    await state.set_state(ComplaintForm.category)

    await callback.message.edit_text(
        "📝 <b>СОЗДАНИЕ ОБРАЩЕНИЯ</b>\n\n"
        "Шаг 1/3\n\n"
        "Выберите категорию:",
        parse_mode="HTML",
        reply_markup=category_menu()
    )

    await callback.answer()


@dp.callback_query(F.data.startswith("category:"))
async def select_category(callback: CallbackQuery, state: FSMContext):

    category = callback.data.split(":", 1)[1]

    await state.update_data(category=category)
    await state.set_state(ComplaintForm.target)

    await callback.message.edit_text(
        "🎯 <b>ШАГ 2/3</b>\n\n"
        "Отправьте <code>@username</code> или ссылку "
        "на профиль/канал/группу.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="❌ Отмена",
                        callback_data="cancel"
                    )
                ]
            ]
        )
    )

    await callback.answer()


@dp.message(ComplaintForm.target)
async def complaint_target(message: Message, state: FSMContext):

    target = message.text.strip()

    if len(target) < 2:
        await message.answer(
            "⚠️ Укажите корректный username или ссылку."
        )
        return

    await state.update_data(target=target)
    await state.set_state(ComplaintForm.details)

    await message.answer(
        "📝 <b>ШАГ 3/3</b>\n\n"
        "Опишите фактическую ситуацию.\n\n"
        "Не добавляйте вымышленные сведения — обращение "
        "будет сформировано на основании вашего текста.",
        parse_mode="HTML"
    )


@dp.message(ComplaintForm.details)
async def complaint_details(message: Message, state: FSMContext):

    details = message.text.strip()

    if len(details) < 10:
        await message.answer(
            "⚠️ Описание слишком короткое.\n"
            "Опишите ситуацию подробнее."
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

    await state.set_state(ComplaintForm.preview)

    await message.answer(
        "👀 <b>ПРЕДПРОСМОТР</b>\n\n"
        f"{generated}\n\n"
        "Отправить это обращение в историю?",
        parse_mode="HTML",
        reply_markup=preview_menu()
    )


# ============================================================
# SAVE
# ============================================================

@dp.callback_query(F.data == "complaint_save")
async def complaint_save(callback: CallbackQuery, state: FSMContext):

    if not await has_subscription(callback.from_user.id):
        await callback.answer(
            "❌ Подписка закончилась.",
            show_alert=True
        )
        await state.clear()
        return

    data = await state.get_data()

    await db.execute("""
        INSERT INTO complaints (
            telegram_id,
            category,
            target,
            details,
            generated_text,
            status
        )
        VALUES ($1, $2, $3, $4, $5, 'SAVED')
    """,
        callback.from_user.id,
        data["category"],
        data["target"],
        data["details"],
        data["generated"],
    )

    await state.clear()

    await callback.message.edit_text(
        "✅ <b>Обращение сохранено.</b>\n\n"
        "Оно находится в разделе «Мои обращения».",
        parse_mode="HTML",
        reply_markup=main_menu()
    )

    await callback.answer("Сохранено")


# ============================================================
# EDIT
# ============================================================

@dp.callback_query(F.data == "complaint_edit")
async def complaint_edit(callback: CallbackQuery, state: FSMContext):

    await state.set_state(ComplaintForm.details)

    await callback.message.edit_text(
        "✏️ Отправьте новое описание ситуации:"
    )

    await callback.answer()


# ============================================================
# MY COMPLAINTS
# ============================================================

@dp.callback_query(F.data == "my_complaints")
async def my_complaints(callback: CallbackQuery):

    rows = await db.fetch("""
        SELECT id, category, target, status, created_at
        FROM complaints
        WHERE telegram_id = $1
        ORDER BY id DESC
        LIMIT 10
    """, callback.from_user.id)

    if not rows:
        text = (
            "📋 <b>МОИ ОБРАЩЕНИЯ</b>\n\n"
            "У вас пока нет обращений."
        )
    else:
        lines = ["📋 <b>МОИ ОБРАЩЕНИЯ</b>\n"]

        for row in rows:
            lines.append(
                f"#{row['id']} | "
                f"{CATEGORIES.get(row['category'], row['category'])}\n"
                f"🎯 {row['target']}\n"
                f"📌 {row['status']}\n"
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
async def help_menu(callback: CallbackQuery):

    await callback.message.edit_text(
        "ℹ️ <b>FENIX REPORT — ПОМОЩЬ</b>\n\n"
        "1️⃣ Нажмите «Создать обращение».\n"
        "2️⃣ Выберите категорию.\n"
        "3️⃣ Укажите @username или ссылку.\n"
        "4️⃣ Опишите фактическую ситуацию.\n"
        "5️⃣ Проверьте автоматически сформированный текст.\n"
        "6️⃣ Сохраните обращение.\n\n"
        "Все обращения сохраняются в PostgreSQL.",
        parse_mode="HTML",
        reply_markup=main_menu()
    )

    await callback.answer()


# ============================================================
# CANCEL
# ============================================================

@dp.callback_query(F.data == "cancel")
async def cancel(callback: CallbackQuery, state: FSMContext):

    await state.clear()

    await callback.message.edit_text(
        "❌ Операция отменена.",
        reply_markup=main_menu()
    )

    await callback.answer()


@dp.callback_query(F.data == "back_main")
async def back_main(callback: CallbackQuery, state: FSMContext):

    await state.clear()

    await callback.message.edit_text(
        "🔥 <b>FENIX REPORT</b>\n\n"
        "Главное меню:",
        parse_mode="HTML",
        reply_markup=main_menu()
    )

    await callback.answer()


# ============================================================
# ADMIN CHECK
# ============================================================

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


async def admin_required(callback: CallbackQuery) -> bool:

    if not is_admin(callback.from_user.id):
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
async def admin_command(message: Message):

    await upsert_user(message)

    if not is_admin(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
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
async def admin_stats(callback: CallbackQuery):

    if not await admin_required(callback):
        return

    users = await db.fetchval("""
        SELECT COUNT(*) FROM users
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
        SELECT COUNT(*) FROM complaints
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
        f"📋 Обращений: <b>{complaints}</b>\n"
        f"💾 Сохранённых: <b>{saved}</b>",
        parse_mode="HTML",
        reply_markup=admin_menu()
    )

    await callback.answer()


# ============================================================
# ADMIN USERS
# ============================================================

@dp.callback_query(F.data == "admin_users")
async def admin_users(callback: CallbackQuery):

    if not await admin_required(callback):
        return

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
        LIMIT 15
    """)

    if not rows:
        text = "👥 Пользователей пока нет."
    else:
        lines = ["👥 <b>ПОСЛЕДНИЕ ПОЛЬЗОВАТЕЛИ</b>\n"]

        for row in rows:
            status = "✅" if row["active"] else "❌"

            username = (
                f"@{row['username']}"
                if row["username"]
                else "без username"
            )

            lines.append(
                f"{status} <code>{row['telegram_id']}</code> "
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
# GIVE SUBSCRIPTION
# ============================================================

@dp.callback_query(F.data == "admin_give_sub")
async def admin_give_sub(callback: CallbackQuery, state: FSMContext):

    if not await admin_required(callback):
        return

    await state.set_state(AdminSubscription.user_id)

    await callback.message.edit_text(
        "➕ <b>ВЫДАТЬ ПОДПИСКУ</b>\n\n"
        "Введите Telegram ID пользователя:",
        parse_mode="HTML"
    )

    await callback.answer()


@dp.message(AdminSubscription.user_id)
async def admin_subscription_user(
    message: Message,
    state: FSMContext
):

    try:
        user_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ ID должен быть числом.")
        return

    await state.update_data(user_id=user_id)
    await state.set_state(AdminSubscription.duration)

    await message.answer(
        "⏱ Выберите срок подписки:\n\n"
        "1 — 1 день\n"
        "7 — 7 дней\n"
        "30 — 30 дней\n"
        "0 — навсегда\n\n"
        "Введите число:"
    )


@dp.message(AdminSubscription.duration)
async def admin_subscription_duration(
    message: Message,
    state: FSMContext
):

    try:
        days = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введите число.")
        return

    if days < 0:
        await message.answer("❌ Неверный срок.")
        return

    data = await state.get_data()
    user_id = data["user_id"]

    await db.execute("""
        INSERT INTO users (
            telegram_id
        )
        VALUES ($1)
        ON CONFLICT DO NOTHING
    """, user_id)

    if days == 0:
        expires_at = None
    else:
        expires_at = datetime.now(timezone.utc) + timedelta(days=days)

    await db.execute("""
        INSERT INTO subscriptions (
            telegram_id,
            active,
            expires_at
        )
        VALUES ($1, TRUE, $2)
        ON CONFLICT (telegram_id)
        DO UPDATE SET
            active = TRUE,
            expires_at = EXCLUDED.expires_at
    """,
        user_id,
        expires_at
    )

    await state.clear()

    expiration = (
        "навсегда"
        if expires_at is None
        else expires_at.strftime("%d.%m.%Y %H:%M")
    )

    await message.answer(
        "✅ <b>Подписка выдана.</b>\n\n"
        f"ID: <code>{user_id}</code>\n"
        f"До: <b>{expiration}</b>",
        parse_mode="HTML",
        reply_markup=admin_menu()
    )

    try:
        await bot.send_message(
            user_id,
            "🔥 <b>FENIX REPORT</b>\n\n"
            "Вам выдан доступ к системе.",
            parse_mode="HTML",
            reply_markup=main_menu()
        )
    except Exception as e:
        logger.warning(
            "Не удалось уведомить пользователя %s: %s",
            user_id,
            e
        )


# ============================================================
# REMOVE SUBSCRIPTION
# ============================================================

@dp.callback_query(F.data == "admin_remove_sub")
async def admin_remove_sub(
    callback: CallbackQuery,
    state: FSMContext
):

    if not await admin_required(callback):
        return

    await state.set_state(AdminSearch.user_id)

    await callback.message.edit_text(
        "➖ <b>ЗАБРАТЬ ПОДПИСКУ</b>\n\n"
        "Введите Telegram ID:",
        parse_mode="HTML"
    )

    await callback.answer()


@dp.message(AdminSearch.user_id)
async def admin_remove_sub_user(
    message: Message,
    state: FSMContext
):

    try:
        user_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ ID должен быть числом.")
        return

    await db.execute("""
        UPDATE subscriptions
        SET active = FALSE,
            expires_at = NOW()
        WHERE telegram_id = $1
    """, user_id)

    await state.clear()

    await message.answer(
        "✅ Подписка отключена.\n\n"
        f"ID: <code>{user_id}</code>",
        parse_mode="HTML",
        reply_markup=admin_menu()
    )


# ============================================================
# ADMIN SEARCH
# ============================================================

@dp.callback_query(F.data == "admin_search")
async def admin_search(callback: CallbackQuery, state: FSMContext):

    if not await admin_required(callback):
        return

    await state.set_state(AdminSearch.user_id)

    await callback.message.edit_text(
        "🔎 <b>ПОИСК</b>\n\n"
        "Введите Telegram ID пользователя:",
        parse_mode="HTML"
    )

    await callback.answer()


# ============================================================
# ADMIN COMPLAINTS
# ============================================================

@dp.callback_query(F.data == "admin_complaints")
async def admin_complaints(callback: CallbackQuery):

    if not await admin_required(callback):
        return

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
        text = "📋 Обращений пока нет."
    else:
        lines = ["📋 <b>ПОСЛЕДНИЕ ОБРАЩЕНИЯ</b>\n"]

        for row in rows:
            lines.append(
                f"#{row['id']} | "
                f"<code>{row['telegram_id']}</code>\n"
                f"{CATEGORIES.get(row['category'], row['category'])}\n"
                f"🎯 {row['target']}\n"
                f"📌 {row['status']}\n"
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

    await state.set_state(AdminBroadcast.text)

    await callback.message.edit_text(
        "📢 <b>РАССЫЛКА</b>\n\n"
        "Введите текст сообщения:",
        parse_mode="HTML"
    )

    await callback.answer()


@dp.message(AdminBroadcast.text)
async def admin_broadcast_send(
    message: Message,
    state: FSMContext
):

    if message.from_user.id != ADMIN_ID:
        return

    text = message.text

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

            await asyncio.sleep(0.05)

        except Exception:
            failed += 1

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
async def fallback(message: Message):

    await upsert_user(message)

    if not await has_subscription(message.from_user.id):
        await message.answer(
            "🔐 Для доступа необходима активная подписка.",
            reply_markup=subscription_menu()
        )
        return

    await message.answer(
        "Используйте кнопки меню:",
        reply_markup=main_menu()
    )


# ============================================================
# STARTUP
# ============================================================

async def main():

    global db

    await init_db()

    logger.info("Fenix Report starting...")

    try:
        await dp.start_polling(bot)
    finally:
        if db:
            await db.close()

        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())