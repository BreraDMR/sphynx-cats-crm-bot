from __future__ import annotations

import os
import sys
import html
import logging
import asyncio
from pathlib import Path

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    BotCommand, BotCommandScopeDefault, BotCommandScopeChat
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import (
    init_db, get_admin, register_admin, is_login_taken,
    get_all_admins, update_admin_status, add_audit_log, get_audit_log,
    get_model_pref, set_model_pref
)
from catalog_api import NewCat, create_cat, list_cats, delete_cat, CatalogApiError
from treats_api import NewTreat, create_treat, list_treats, delete_treat, TreatsApiError
from ai_review import review_description, AiReviewUnavailable
from requests_api import list_requests, RequestsApiError
from notify_server import create_notify_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("sphynx_crm")


def load_env() -> None:
    env_path = Path(".env")
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())


load_env()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_TELEGRAM_ID", "0") or "0")
BOT_API_KEY = os.getenv("BOT_API_KEY", "")
SITE_API_URL = os.getenv("SITE_API_URL", "")
SITE_REQUESTS_API_URL = os.getenv("SITE_REQUESTS_API_URL", "")
SITE_TREATS_API_URL = os.getenv("SITE_TREATS_API_URL", "")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama:11434")
# Two AI models: the fast/weak default and a smarter/slower one. Each admin
# picks which to use via /model (stored per-user); the choice is resolved by
# resolve_model() and passed to the Ollama description review.
CHAT_MODEL = os.getenv("CHAT_MODEL", "qwen2.5:3b")
CHAT_MODEL_STRONG = os.getenv("CHAT_MODEL_STRONG", "qwen2.5:14b")
NOTIFY_PORT = int(os.getenv("NOTIFY_PORT", "8080"))

if not TOKEN or TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
    logger.error("TELEGRAM_BOT_TOKEN is not set. Please update your .env file.")
    sys.exit("Error: TELEGRAM_BOT_TOKEN is not configured.")

if not OWNER_ID:
    logger.error("OWNER_TELEGRAM_ID is not set in .env!")

if not BOT_API_KEY or not SITE_API_URL:
    logger.warning("BOT_API_KEY/SITE_API_URL are not fully configured -- catalog commands will fail.")

# Same color list as CatValidator::COLORS on the website (site/src/CatValidator.php) --
# the two projects don't share code, so keep these two lists in sync by hand.
COLORS = ["чорний", "білий", "блакитний", "кремовий", "лиловий", "інший"]

# Treat categories: canonical keys must match TreatValidator::CATEGORIES on the
# website (site/src/TreatValidator.php); the Ukrainian labels are just for the
# bot's keyboard. Keep the keys in sync by hand.
TREAT_CATEGORIES = {
    "snacks": "Ласощі",
    "food": "Корм",
    "vitamins": "Вітаміни",
    "toys": "Іграшки",
    "care": "Догляд",
}

# Human labels for the two model tiers (shown in /model).
MODEL_LABELS = {"weak": "Слабка (швидка)", "strong": "Сильна (розумніша)"}


def resolve_model(user_id: int) -> str:
    """Map a user's 'weak'/'strong' preference to the actual Ollama model name."""
    return CHAT_MODEL_STRONG if get_model_pref(user_id) == "strong" else CHAT_MODEL

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

http_session: aiohttp.ClientSession | None = None


class RegisterStates(StatesGroup):
    waiting_for_login = State()
    waiting_for_password = State()


class AddCatStates(StatesGroup):
    waiting_name = State()
    waiting_color = State()
    waiting_age = State()
    waiting_price = State()
    waiting_description = State()
    reviewing_description = State()
    waiting_photo = State()


class AddTreatStates(StatesGroup):
    waiting_name = State()
    waiting_category = State()
    waiting_price = State()
    waiting_weight = State()
    waiting_description = State()
    reviewing_description = State()
    waiting_photo = State()


# --- ROLE HELPERS ---

def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


def admin_status(user_id: int) -> str | None:
    """Returns 'owner', 'approved', 'pending_approval', 'rejected', 'banned', or None."""
    if is_owner(user_id):
        return "owner"
    row = get_admin(user_id)
    return row["status"] if row else None


def is_admin(user_id: int) -> bool:
    return admin_status(user_id) in ("owner", "approved")


def get_notification_targets() -> list[int]:
    """Owner + every approved admin -- who a new site request gets pushed to."""
    targets = {OWNER_ID} if OWNER_ID else set()
    targets.update(a["telegram_id"] for a in get_all_admins() if a["status"] == "approved")
    return list(targets)


def actor_label(user) -> str:
    return f"@{user.username}" if user.username else user.full_name


async def check_admin_access(message: Message) -> bool:
    status = admin_status(message.from_user.id)
    if status in ("owner", "approved"):
        return True
    if status is None:
        await message.reply("🔒 Ця команда лише для адмінів розплідника.\nЩоб подати заявку — /register")
    elif status == "pending_approval":
        await message.reply("⏳ Вашу заявку ще не розглянув власник бота.")
    else:
        await message.reply("❌ Доступ заблоковано.")
    return False


# --- COMMAND MENU ---

def build_command_list(role: str | None) -> list[BotCommand]:
    commands = [
        BotCommand(command="start", description="Вітання та статус доступу"),
        BotCommand(command="help", description="Довідка по командах"),
        BotCommand(command="whoami", description="Мій статус"),
    ]
    if role in ("owner", "approved"):
        commands += [
            BotCommand(command="add_cat", description="Додати кошеня в каталог"),
            BotCommand(command="list_cats", description="Список кошенят (усі статуси)"),
            BotCommand(command="delete_cat", description="Видалити кошеня за ID"),
            BotCommand(command="add_treat", description="Додати смаколик у каталог"),
            BotCommand(command="list_treats", description="Список смаколиків (усі статуси)"),
            BotCommand(command="delete_treat", description="Видалити смаколик за ID"),
            BotCommand(command="model", description="Вибір моделі ШІ для перевірки тексту"),
            BotCommand(command="requests", description="Останні заявки з сайту"),
        ]
    if role == "owner":
        commands += [
            BotCommand(command="admins", description="Керування адмінами"),
            BotCommand(command="auditlog", description="Журнал дій"),
        ]
    if role is None:
        commands.append(BotCommand(command="register", description="Подати заявку на доступ"))
    return commands


async def apply_user_commands(telegram_id: int, role: str | None) -> None:
    try:
        await bot.set_my_commands(build_command_list(role), scope=BotCommandScopeChat(chat_id=telegram_id))
    except Exception:
        logger.exception(f"Failed to set command menu for {telegram_id}")


# --- /start, /help, /whoami ---

@dp.message(CommandStart())
async def command_start_handler(message: Message) -> None:
    status = admin_status(message.from_user.id)
    text = "👋 <b>Sphynx Cats CRM Bot</b>\nДодавання карток кошенят на сайт розплідника.\n\n"

    if status == "owner":
        text += "👑 Ви власник бота. Повний доступ до всіх команд."
    elif status == "approved":
        text += "✅ Ви адміністратор каталогу. /add_cat — додати кошеня."
    elif status == "pending_approval":
        text += "⏳ Заявку на доступ подано, очікуйте підтвердження власника."
    elif status == "rejected":
        text += "❌ Заявку на доступ відхилено власником."
    elif status == "banned":
        text += "🚫 Доступ заблоковано."
    else:
        text += "🔒 Доступ закрито. Подати заявку — /register"

    text += "\n\nℹ️ /help — список команд"
    menu_role = status if status not in ("rejected", "banned") else None
    await apply_user_commands(message.from_user.id, menu_role)
    await message.reply(text)


@dp.message(Command("help"))
async def command_help_handler(message: Message) -> None:
    status = admin_status(message.from_user.id)
    lines = ["ℹ️ <b>Команди</b>\n"]
    lines.append("▫️ /start — вітання та статус")
    lines.append("▫️ /whoami — мій статус")
    if status in ("owner", "approved"):
        lines.append("🐱 /add_cat — додати кошеня (з AI-перевіркою опису)")
        lines.append("📋 /list_cats — список усіх кошенят")
        lines.append("🗑️ /delete_cat &lt;id&gt; — видалити кошеня")
        lines.append("🍖 /add_treat — додати смаколик (з AI-перевіркою опису)")
        lines.append("📋 /list_treats — список усіх смаколиків")
        lines.append("🗑️ /delete_treat &lt;id&gt; — видалити смаколик")
        lines.append("🤖 /model — вибрати модель ШІ (слабка/сильна)")
        lines.append("📨 /requests — останні заявки з сайту (теж приходять автоматично)")
    if status == "owner":
        lines.append("👑 /admins — підтвердження/бан адмінів")
        lines.append("📜 /auditlog — журнал дій")
    if status is None:
        lines.append("📝 /register — подати заявку на доступ")
    await message.reply("\n".join(lines))


@dp.message(Command("whoami"))
async def command_whoami_handler(message: Message) -> None:
    status = admin_status(message.from_user.id) or "не зареєстровано"
    await message.reply(f"🆔 Ваш Telegram ID: <code>{message.from_user.id}</code>\n📌 Статус: <b>{html.escape(status)}</b>")


# --- /register (admin self-registration with owner approval) ---

@dp.message(Command("register"))
async def start_registration(message: Message, state: FSMContext) -> None:
    if is_owner(message.from_user.id):
        return await message.reply("👑 Ви власник, реєстрація не потрібна.")

    existing = get_admin(message.from_user.id)
    if existing:
        return await message.reply(f"Ви вже подавали заявку. Статус: <b>{html.escape(existing['status'])}</b>")

    await state.set_state(RegisterStates.waiting_for_login)
    await message.reply("📝 Реєстрація адміністратора каталогу.\nПридумайте та надішліть бажаний <b>логін</b>:")


@dp.message(RegisterStates.waiting_for_login, F.text)
async def process_login(message: Message, state: FSMContext) -> None:
    login = message.text.strip()
    if len(login) < 3:
        return await message.reply("❌ Логін має містити щонайменше 3 символи. Спробуйте ще раз:")
    if is_login_taken(login):
        return await message.reply("❌ Цей логін уже зайнятий. Придумайте інший:")

    await state.update_data(chosen_login=login)
    await state.set_state(RegisterStates.waiting_for_password)
    await message.reply("🔑 Тепер придумайте та надішліть пароль:")


@dp.message(RegisterStates.waiting_for_password, F.text)
async def process_password(message: Message, state: FSMContext) -> None:
    password = message.text.strip()
    if len(password) < 4:
        return await message.reply("❌ Пароль занадто короткий. Придумайте надійніший:")

    data = await state.get_data()
    login = data["chosen_login"]

    success = register_admin(
        telegram_id=message.from_user.id,
        username=message.from_user.username or message.from_user.full_name,
        login=login,
        password_plain=password,
    )
    await state.clear()

    if not success:
        return await message.reply("❌ Сталася помилка. Спробуйте знову через /register")

    await message.reply("🎉 Заявку надіслано власнику бота. Очікуйте підтвердження.")

    if OWNER_ID:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Підтвердити", callback_data=f"approve_admin_{message.from_user.id}"),
            InlineKeyboardButton(text="❌ Відхилити", callback_data=f"reject_admin_{message.from_user.id}"),
        ]])
        try:
            await bot.send_message(
                OWNER_ID,
                f"🔔 <b>Новий запит на доступ до СРМ котів!</b>\n\n"
                f"👤 {html.escape(message.from_user.full_name)}\n"
                f"🏷️ Username: @{html.escape(message.from_user.username or 'none')}\n"
                f"🆔 ID: <code>{message.from_user.id}</code>\n"
                f"📝 Логін: <code>{html.escape(login)}</code>",
                reply_markup=kb,
            )
        except Exception:
            logger.exception("Failed to notify owner about new registration")


@dp.callback_query(F.data.startswith("approve_admin_") | F.data.startswith("reject_admin_"))
async def handle_admin_approval(callback: CallbackQuery) -> None:
    if not is_owner(callback.from_user.id):
        return await callback.answer("Тільки власник бота може це робити.", show_alert=True)

    action, _, target_id = callback.data.rpartition("_admin_")
    target_id = int(target_id)
    label = actor_label(callback.from_user)

    if action == "approve":
        update_admin_status(target_id, "approved")
        add_audit_log(callback.from_user.id, label, "approve_admin", details=str(target_id))
        await apply_user_commands(target_id, "approved")
        await callback.message.edit_text(f"✅ Адміна <code>{target_id}</code> підтверджено.")
        try:
            await bot.send_message(target_id, "🎉 Власник підтвердив ваш доступ! Тепер можна користуватись /add_cat. Деталі — /help")
        except Exception:
            pass
    else:
        update_admin_status(target_id, "rejected")
        add_audit_log(callback.from_user.id, label, "reject_admin", details=str(target_id))
        await callback.message.edit_text(f"❌ Заявку <code>{target_id}</code> відхилено.")
        try:
            await bot.send_message(target_id, "❌ Власник відхилив вашу заявку на доступ до бота.")
        except Exception:
            pass

    await callback.answer()


# --- /admins (owner-only ban/unban panel) ---

def render_admins_panel() -> tuple[str, InlineKeyboardMarkup]:
    admins = get_all_admins()
    if not admins:
        return "👥 Список адмінів порожній.", InlineKeyboardMarkup(inline_keyboard=[])

    icons = {"pending_approval": "⏳", "approved": "✅", "rejected": "❌", "banned": "🚫"}
    report = "👑 <b>АДМІНИ КАТАЛОГУ</b>\n\n"
    kb_rows = []
    for a in admins[:15]:
        icon = icons.get(a["status"], "❔")
        report += f"{icon} <code>{html.escape(a['login'])}</code> | ID: <code>{a['telegram_id']}</code> | {a['status']}\n"
        if a["status"] == "approved":
            kb_rows.append([InlineKeyboardButton(text=f"🚫 Бан {a['login']}", callback_data=f"ban_admin_{a['telegram_id']}")])
        elif a["status"] == "banned":
            kb_rows.append([InlineKeyboardButton(text=f"🟢 Розбан {a['login']}", callback_data=f"unban_admin_{a['telegram_id']}")])

    return report, InlineKeyboardMarkup(inline_keyboard=kb_rows)


@dp.message(Command("admins"))
async def command_admins_handler(message: Message) -> None:
    if not is_owner(message.from_user.id):
        return
    report, kb = render_admins_panel()
    await message.reply(report, reply_markup=kb)


@dp.callback_query(F.data.startswith("ban_admin_") | F.data.startswith("unban_admin_"))
async def handle_admin_ban(callback: CallbackQuery) -> None:
    if not is_owner(callback.from_user.id):
        return await callback.answer("Тільки власник бота може це робити.", show_alert=True)

    action, _, target_id = callback.data.rpartition("_admin_")
    target_id = int(target_id)
    label = actor_label(callback.from_user)

    if action == "ban":
        update_admin_status(target_id, "banned")
        add_audit_log(callback.from_user.id, label, "ban_admin", details=str(target_id))
        await apply_user_commands(target_id, None)
        await callback.answer("Заблоковано.")
    else:
        update_admin_status(target_id, "approved")
        add_audit_log(callback.from_user.id, label, "unban_admin", details=str(target_id))
        await apply_user_commands(target_id, "approved")
        await callback.answer("Розблоковано.")

    report, kb = render_admins_panel()
    await callback.message.edit_text(report, reply_markup=kb)


@dp.message(Command("auditlog"))
async def command_auditlog_handler(message: Message) -> None:
    if not is_owner(message.from_user.id):
        return

    entries = get_audit_log(limit=20)
    if not entries:
        return await message.reply("📜 Журнал дій порожній.")

    lines = ["📜 <b>ЖУРНАЛ ДІЙ</b>\n"]
    for e in entries:
        lines.append(f"🕒 {e['created_at']} — <b>{html.escape(e['actor_label'] or 'unknown')}</b> {e['action']} {html.escape(e['details'] or '')}")

    text = "\n".join(lines)
    for chunk_start in range(0, len(text), 4096):
        await message.reply(text[chunk_start:chunk_start + 4096])


# --- /add_cat ---

def colors_keyboard() -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text=c, callback_data=f"cat_color_{c}")] for c in COLORS]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def description_review_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Використати AI-версію", callback_data="desc_use_ai"),
        InlineKeyboardButton(text="📝 Залишити свою", callback_data="desc_keep_mine"),
    ], [
        InlineKeyboardButton(text="✏️ Написати інший опис", callback_data="desc_retry"),
    ]])


@dp.message(Command("add_cat"))
async def command_add_cat_handler(message: Message, state: FSMContext) -> None:
    if not await check_admin_access(message):
        return
    await state.set_state(AddCatStates.waiting_name)
    await message.reply("🐱 Додаємо нове кошеня.\nНадішліть <b>ім'я</b>:")


@dp.message(AddCatStates.waiting_name, F.text)
async def process_cat_name(message: Message, state: FSMContext) -> None:
    name = message.text.strip()
    if len(name) < 2:
        return await message.reply("❌ Ім'я занадто коротке. Спробуйте ще раз:")
    await state.update_data(name=name)
    await state.set_state(AddCatStates.waiting_color)
    await message.reply("🎨 Оберіть колір:", reply_markup=colors_keyboard())


@dp.callback_query(AddCatStates.waiting_color, F.data.startswith("cat_color_"))
async def process_cat_color(callback: CallbackQuery, state: FSMContext) -> None:
    color = callback.data.removeprefix("cat_color_")
    await state.update_data(color=color)
    await state.set_state(AddCatStates.waiting_age)
    await callback.message.edit_text(f"🎨 Колір: <b>{html.escape(color)}</b>")
    await callback.message.answer("📅 Скільки кошеняті місяців? (число)")
    await callback.answer()


@dp.message(AddCatStates.waiting_age, F.text)
async def process_cat_age(message: Message, state: FSMContext) -> None:
    try:
        age = int(message.text.strip())
        if age <= 0 or age > 240:
            raise ValueError
    except ValueError:
        return await message.reply("❌ Введіть додатнє число місяців (наприклад, 3):")

    await state.update_data(age_months=age)
    await state.set_state(AddCatStates.waiting_price)
    await message.reply("💶 Яка ціна в євро? (число)")


@dp.message(AddCatStates.waiting_price, F.text)
async def process_cat_price(message: Message, state: FSMContext) -> None:
    try:
        price = int(message.text.strip())
        if price <= 0 or price > 100_000:
            raise ValueError
    except ValueError:
        return await message.reply("❌ Введіть додатню ціну в євро (наприклад, 1450):")

    await state.update_data(price_eur=price)
    await state.set_state(AddCatStates.waiting_description)
    await message.reply("📝 Надішліть опис кошеняти (мінімум 10 символів). Перед публікацією я перевірю текст через AI.")


@dp.message(AddCatStates.waiting_description, F.text)
async def process_cat_description(message: Message, state: FSMContext) -> None:
    description = message.text.strip()
    if len(description) < 10:
        return await message.reply("❌ Опис занадто короткий (мінімум 10 символів). Спробуйте ще раз:")

    await state.update_data(original_description=description, final_description=description)

    status_msg = await message.reply("⏳ Перевіряю текст через Ollama...")
    try:
        suggestion = await review_description(http_session, OLLAMA_URL, resolve_model(message.from_user.id), description)
    except AiReviewUnavailable:
        logger.exception("AI review unavailable, falling back to the admin's original text")
        await status_msg.edit_text("⚠️ AI-перевірка зараз недоступна, використовую ваш текст без змін.")
        await state.set_state(AddCatStates.waiting_photo)
        return await message.answer("📷 Надішліть фото кошеняти, або /skip щоб додати без фото.")

    if suggestion.strip() == description.strip():
        await status_msg.edit_text("✅ AI не знайшов помилок у тексті.")
        await state.set_state(AddCatStates.waiting_photo)
        return await message.answer("📷 Надішліть фото кошеняти, або /skip щоб додати без фото.")

    await state.update_data(ai_suggestion=suggestion)
    await state.set_state(AddCatStates.reviewing_description)
    await status_msg.edit_text(
        f"<b>Ваш варіант:</b>\n{html.escape(description)}\n\n"
        f"<b>Варіант AI:</b>\n{html.escape(suggestion)}",
        reply_markup=description_review_keyboard(),
    )


@dp.callback_query(AddCatStates.reviewing_description, F.data.in_(["desc_use_ai", "desc_keep_mine", "desc_retry"]))
async def process_description_choice(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()

    if callback.data == "desc_retry":
        await state.set_state(AddCatStates.waiting_description)
        await callback.message.edit_text("✏️ Надішліть новий опис кошеняти:")
        return await callback.answer()

    final = data["ai_suggestion"] if callback.data == "desc_use_ai" else data["original_description"]
    await state.update_data(final_description=final)
    await state.set_state(AddCatStates.waiting_photo)
    await callback.message.edit_text(f"📝 Опис збережено:\n{html.escape(final)}")
    await callback.message.answer("📷 Надішліть фото кошеняти, або /skip щоб додати без фото.")
    await callback.answer()


async def finalize_cat_creation(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    cat = NewCat(
        name=data["name"],
        color=data["color"],
        age_months=data["age_months"],
        price_eur=data["price_eur"],
        description=data["final_description"],
        created_by=f"bot:{actor_label(message.from_user)}",
        photo_bytes=data.get("photo_bytes"),
        photo_filename=data.get("photo_filename"),
    )

    try:
        result = await create_cat(http_session, SITE_API_URL, BOT_API_KEY, cat)
    except CatalogApiError as e:
        await message.reply(f"❌ Не вдалося додати кошеня: {html.escape(str(e))}")
        await state.clear()
        return

    created = result.get("cat", {})
    add_audit_log(message.from_user.id, actor_label(message.from_user), "add_cat", details=f"id={created.get('id')} name={cat.name}")
    await message.reply(
        f"🎉 Кошеня додано на сайт!\n"
        f"🆔 ID: <code>{created.get('id')}</code>\n"
        f"🔗 Slug: <code>{html.escape(created.get('slug', ''))}</code>"
    )
    await state.clear()


@dp.message(AddCatStates.waiting_photo, F.photo)
async def process_cat_photo(message: Message, state: FSMContext) -> None:
    photo = message.photo[-1]
    buffer = await bot.download(photo)
    await state.update_data(photo_bytes=buffer.read(), photo_filename=f"{photo.file_unique_id}.jpg")
    await finalize_cat_creation(message, state)


@dp.message(AddCatStates.waiting_photo, Command("skip"))
async def process_cat_skip_photo(message: Message, state: FSMContext) -> None:
    await finalize_cat_creation(message, state)


# --- /list_cats, /delete_cat ---

@dp.message(Command("list_cats"))
async def command_list_cats_handler(message: Message) -> None:
    if not await check_admin_access(message):
        return

    try:
        cats = await list_cats(http_session, SITE_API_URL, BOT_API_KEY)
    except CatalogApiError as e:
        return await message.reply(f"❌ {html.escape(str(e))}")

    if not cats:
        return await message.reply("📋 Каталог порожній.")

    status_icons = {"published": "✅", "draft": "📝"}
    lines = ["📋 <b>КОШЕНЯТА В КАТАЛОЗІ</b>\n"]
    for c in cats:
        icon = status_icons.get(c["status"], "❔")
        lines.append(f"{icon} <code>#{c['id']}</code> {html.escape(c['name'])} — {html.escape(c['color'])}, {c['price_eur']}€")

    text = "\n".join(lines)
    for chunk_start in range(0, len(text), 4096):
        await message.reply(text[chunk_start:chunk_start + 4096])


@dp.message(Command("delete_cat"))
async def command_delete_cat_handler(message: Message) -> None:
    if not await check_admin_access(message):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip().isdigit():
        return await message.reply("Використання: <code>/delete_cat ID</code>")

    cat_id = int(parts[1].strip())
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Видалити", callback_data=f"delcat_yes_{cat_id}"),
        InlineKeyboardButton(text="❌ Скасувати", callback_data=f"delcat_no_{cat_id}"),
    ]])
    await message.reply(f"Видалити кошеня <code>#{cat_id}</code> з каталогу?", reply_markup=kb)


@dp.callback_query(F.data.startswith("delcat_yes_") | F.data.startswith("delcat_no_"))
async def handle_delete_cat_confirm(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return await callback.answer("Немає прав.", show_alert=True)

    action, _, cat_id = callback.data.rpartition("_")
    cat_id = int(cat_id)

    if action.endswith("yes"):
        try:
            await delete_cat(http_session, SITE_API_URL, BOT_API_KEY, cat_id)
        except CatalogApiError as e:
            await callback.message.edit_text(f"❌ {html.escape(str(e))}")
            return await callback.answer()
        add_audit_log(callback.from_user.id, actor_label(callback.from_user), "delete_cat", details=f"id={cat_id}")
        await callback.message.edit_text(f"🗑️ Кошеня <code>#{cat_id}</code> видалено.")
    else:
        await callback.message.edit_text("Скасовано.")

    await callback.answer()


# --- /model (per-admin AI model choice) ---

def model_keyboard(current: str) -> InlineKeyboardMarkup:
    def label(key: str) -> str:
        mark = "✅ " if key == current else ""
        return mark + MODEL_LABELS[key]
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label("weak"), callback_data="model_set_weak"),
        InlineKeyboardButton(text=label("strong"), callback_data="model_set_strong"),
    ]])


@dp.message(Command("model"))
async def command_model_handler(message: Message) -> None:
    if not await check_admin_access(message):
        return
    pref = get_model_pref(message.from_user.id)
    model_name = CHAT_MODEL_STRONG if pref == "strong" else CHAT_MODEL
    await message.reply(
        "🤖 <b>Модель ШІ для перевірки опису</b>\n\n"
        f"Зараз обрано: <b>{html.escape(MODEL_LABELS[pref])}</b> (<code>{html.escape(model_name)}</code>)\n\n"
        "▫️ <b>Слабка</b> — швидша, але простіша.\n"
        "▫️ <b>Сильна</b> — розумніша, але відповідає повільніше.\n"
        "Швидкість не критична — обирай ту, що дає кращий текст.",
        reply_markup=model_keyboard(pref),
    )


@dp.callback_query(F.data.in_(["model_set_weak", "model_set_strong"]))
async def handle_model_choice(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return await callback.answer("Немає прав.", show_alert=True)

    pref = "strong" if callback.data == "model_set_strong" else "weak"
    set_model_pref(callback.from_user.id, pref)
    model_name = CHAT_MODEL_STRONG if pref == "strong" else CHAT_MODEL
    add_audit_log(callback.from_user.id, actor_label(callback.from_user), "set_model", details=pref)

    await callback.message.edit_text(
        "🤖 <b>Модель ШІ для перевірки опису</b>\n\n"
        f"Обрано: <b>{html.escape(MODEL_LABELS[pref])}</b> (<code>{html.escape(model_name)}</code>)",
        reply_markup=model_keyboard(pref),
    )
    await callback.answer("Збережено.")


# --- /add_treat ---

def treat_categories_keyboard() -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text=label, callback_data=f"treat_cat_{key}")] for key, label in TREAT_CATEGORIES.items()]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.message(Command("add_treat"))
async def command_add_treat_handler(message: Message, state: FSMContext) -> None:
    if not await check_admin_access(message):
        return
    await state.set_state(AddTreatStates.waiting_name)
    await message.reply("🍖 Додаємо новий смаколик.\nНадішліть <b>назву</b>:")


@dp.message(AddTreatStates.waiting_name, F.text)
async def process_treat_name(message: Message, state: FSMContext) -> None:
    name = message.text.strip()
    if len(name) < 2:
        return await message.reply("❌ Назва занадто коротка. Спробуйте ще раз:")
    await state.update_data(name=name)
    await state.set_state(AddTreatStates.waiting_category)
    await message.reply("🏷️ Оберіть категорію:", reply_markup=treat_categories_keyboard())


@dp.callback_query(AddTreatStates.waiting_category, F.data.startswith("treat_cat_"))
async def process_treat_category(callback: CallbackQuery, state: FSMContext) -> None:
    category = callback.data.removeprefix("treat_cat_")
    await state.update_data(category=category)
    await state.set_state(AddTreatStates.waiting_price)
    await callback.message.edit_text(f"🏷️ Категорія: <b>{html.escape(TREAT_CATEGORIES.get(category, category))}</b>")
    await callback.message.answer("💶 Яка ціна в євро? (число)")
    await callback.answer()


@dp.message(AddTreatStates.waiting_price, F.text)
async def process_treat_price(message: Message, state: FSMContext) -> None:
    try:
        price = int(message.text.strip())
        if price <= 0 or price > 100_000:
            raise ValueError
    except ValueError:
        return await message.reply("❌ Введіть додатню ціну в євро (наприклад, 12):")

    await state.update_data(price_eur=price)
    await state.set_state(AddTreatStates.waiting_weight)
    await message.reply("⚖️ Вага в грамах? (число; 0 — якщо не застосовно, напр. для іграшок)")


@dp.message(AddTreatStates.waiting_weight, F.text)
async def process_treat_weight(message: Message, state: FSMContext) -> None:
    try:
        weight = int(message.text.strip())
        if weight < 0 or weight > 100_000:
            raise ValueError
    except ValueError:
        return await message.reply("❌ Введіть вагу в грамах (0 або додатнє число):")

    await state.update_data(weight_g=weight)
    await state.set_state(AddTreatStates.waiting_description)
    await message.reply("📝 Надішліть опис смаколика (мінімум 10 символів). Перед публікацією я перевірю текст через AI.")


@dp.message(AddTreatStates.waiting_description, F.text)
async def process_treat_description(message: Message, state: FSMContext) -> None:
    description = message.text.strip()
    if len(description) < 10:
        return await message.reply("❌ Опис занадто короткий (мінімум 10 символів). Спробуйте ще раз:")

    await state.update_data(original_description=description, final_description=description)

    status_msg = await message.reply("⏳ Перевіряю текст через Ollama...")
    try:
        suggestion = await review_description(http_session, OLLAMA_URL, resolve_model(message.from_user.id), description)
    except AiReviewUnavailable:
        logger.exception("AI review unavailable, falling back to the admin's original text")
        await status_msg.edit_text("⚠️ AI-перевірка зараз недоступна, використовую ваш текст без змін.")
        await state.set_state(AddTreatStates.waiting_photo)
        return await message.answer("📷 Надішліть фото смаколика, або /skip щоб додати без фото.")

    if suggestion.strip() == description.strip():
        await status_msg.edit_text("✅ AI не знайшов помилок у тексті.")
        await state.set_state(AddTreatStates.waiting_photo)
        return await message.answer("📷 Надішліть фото смаколика, або /skip щоб додати без фото.")

    await state.update_data(ai_suggestion=suggestion)
    await state.set_state(AddTreatStates.reviewing_description)
    await status_msg.edit_text(
        f"<b>Ваш варіант:</b>\n{html.escape(description)}\n\n"
        f"<b>Варіант AI:</b>\n{html.escape(suggestion)}",
        reply_markup=description_review_keyboard(),
    )


@dp.callback_query(AddTreatStates.reviewing_description, F.data.in_(["desc_use_ai", "desc_keep_mine", "desc_retry"]))
async def process_treat_description_choice(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()

    if callback.data == "desc_retry":
        await state.set_state(AddTreatStates.waiting_description)
        await callback.message.edit_text("✏️ Надішліть новий опис смаколика:")
        return await callback.answer()

    final = data["ai_suggestion"] if callback.data == "desc_use_ai" else data["original_description"]
    await state.update_data(final_description=final)
    await state.set_state(AddTreatStates.waiting_photo)
    await callback.message.edit_text(f"📝 Опис збережено:\n{html.escape(final)}")
    await callback.message.answer("📷 Надішліть фото смаколика, або /skip щоб додати без фото.")
    await callback.answer()


async def finalize_treat_creation(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    treat = NewTreat(
        name=data["name"],
        category=data["category"],
        price_eur=data["price_eur"],
        weight_g=data["weight_g"],
        description=data["final_description"],
        created_by=f"bot:{actor_label(message.from_user)}",
        photo_bytes=data.get("photo_bytes"),
        photo_filename=data.get("photo_filename"),
    )

    try:
        result = await create_treat(http_session, SITE_TREATS_API_URL, BOT_API_KEY, treat)
    except TreatsApiError as e:
        await message.reply(f"❌ Не вдалося додати смаколик: {html.escape(str(e))}")
        await state.clear()
        return

    created = result.get("treat", {})
    add_audit_log(message.from_user.id, actor_label(message.from_user), "add_treat", details=f"id={created.get('id')} name={treat.name}")
    await message.reply(
        f"🎉 Смаколик додано на сайт!\n"
        f"🆔 ID: <code>{created.get('id')}</code>\n"
        f"🔗 Slug: <code>{html.escape(created.get('slug', ''))}</code>"
    )
    await state.clear()


@dp.message(AddTreatStates.waiting_photo, F.photo)
async def process_treat_photo(message: Message, state: FSMContext) -> None:
    photo = message.photo[-1]
    buffer = await bot.download(photo)
    await state.update_data(photo_bytes=buffer.read(), photo_filename=f"{photo.file_unique_id}.jpg")
    await finalize_treat_creation(message, state)


@dp.message(AddTreatStates.waiting_photo, Command("skip"))
async def process_treat_skip_photo(message: Message, state: FSMContext) -> None:
    await finalize_treat_creation(message, state)


# --- /list_treats, /delete_treat ---

@dp.message(Command("list_treats"))
async def command_list_treats_handler(message: Message) -> None:
    if not await check_admin_access(message):
        return

    try:
        treats = await list_treats(http_session, SITE_TREATS_API_URL, BOT_API_KEY)
    except TreatsApiError as e:
        return await message.reply(f"❌ {html.escape(str(e))}")

    if not treats:
        return await message.reply("📋 Каталог смаколиків порожній.")

    status_icons = {"published": "✅", "draft": "📝"}
    lines = ["📋 <b>СМАКОЛИКИ В КАТАЛОЗІ</b>\n"]
    for t in treats:
        icon = status_icons.get(t["status"], "❔")
        cat = TREAT_CATEGORIES.get(t["category"], t["category"])
        lines.append(f"{icon} <code>#{t['id']}</code> {html.escape(t['name'])} — {html.escape(cat)}, {t['price_eur']}€")

    text = "\n".join(lines)
    for chunk_start in range(0, len(text), 4096):
        await message.reply(text[chunk_start:chunk_start + 4096])


@dp.message(Command("delete_treat"))
async def command_delete_treat_handler(message: Message) -> None:
    if not await check_admin_access(message):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip().isdigit():
        return await message.reply("Використання: <code>/delete_treat ID</code>")

    treat_id = int(parts[1].strip())
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Видалити", callback_data=f"deltreat_yes_{treat_id}"),
        InlineKeyboardButton(text="❌ Скасувати", callback_data=f"deltreat_no_{treat_id}"),
    ]])
    await message.reply(f"Видалити смаколик <code>#{treat_id}</code> з каталогу?", reply_markup=kb)


@dp.callback_query(F.data.startswith("deltreat_yes_") | F.data.startswith("deltreat_no_"))
async def handle_delete_treat_confirm(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return await callback.answer("Немає прав.", show_alert=True)

    action, _, treat_id = callback.data.rpartition("_")
    treat_id = int(treat_id)

    if action.endswith("yes"):
        try:
            await delete_treat(http_session, SITE_TREATS_API_URL, BOT_API_KEY, treat_id)
        except TreatsApiError as e:
            await callback.message.edit_text(f"❌ {html.escape(str(e))}")
            return await callback.answer()
        add_audit_log(callback.from_user.id, actor_label(callback.from_user), "delete_treat", details=f"id={treat_id}")
        await callback.message.edit_text(f"🗑️ Смаколик <code>#{treat_id}</code> видалено.")
    else:
        await callback.message.edit_text("Скасовано.")

    await callback.answer()


# --- /requests ---

@dp.message(Command("requests"))
async def command_requests_handler(message: Message) -> None:
    if not await check_admin_access(message):
        return

    try:
        reqs = await list_requests(http_session, SITE_REQUESTS_API_URL, BOT_API_KEY)
    except RequestsApiError as e:
        return await message.reply(f"❌ {html.escape(str(e))}")

    if not reqs:
        return await message.reply("📨 Заявок поки немає.")

    lines = ["📨 <b>ОСТАННІ ЗАЯВКИ З САЙТУ</b>\n"]
    for r in reqs:
        lines.append(
            f"🆔 <code>{r['id']}</code> | {html.escape(r['name'])} | {html.escape(r['email'])}"
            f"{' | ' + html.escape(r['phone']) if r.get('phone') else ''} | {html.escape(r['status'])}"
        )

    text = "\n".join(lines)
    for chunk_start in range(0, len(text), 4096):
        await message.reply(text[chunk_start:chunk_start + 4096])


# --- STARTUP ---

async def main() -> None:
    global http_session
    init_db()

    await bot.set_my_commands(build_command_list(None), scope=BotCommandScopeDefault())
    if OWNER_ID:
        await apply_user_commands(OWNER_ID, "owner")

    http_session = aiohttp.ClientSession()

    # The notify server runs in the same process as the long-polling bot --
    # it's how api.php on the website pushes new contact-form requests here
    # (see notify_server.py). Internal-only: reachable from other containers
    # on the same Docker network, never published to the host/internet.
    notify_app = create_notify_app(bot, BOT_API_KEY, get_notification_targets)
    runner = web.AppRunner(notify_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", NOTIFY_PORT)
    await site.start()
    logger.info(f"Notify server listening on :{NOTIFY_PORT}")

    logger.info("Starting Sphynx Cats CRM bot polling...")
    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()
        await http_session.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
