"""
Telegram-бот: школа курсов (напр. маникюр) — продажа курсов с уроками.

Стек: Python 3.10+, aiogram 3.26.0, SQLite (aiosqlite), HTML-разметка.
Платежи: CryptoBot (Crypto Pay API) + ручной перевод по реквизитам.
Фичи: промокоды на скидку, реферальная программа с % продавцу.

Запуск: python bot.py   (см. .env.example)
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import secrets as pysecrets
import string
from contextlib import suppress
from typing import Any, Optional

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ContentType, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
    ReplyParameters,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

# --------------------------------------------------------------------------- #
# Конфигурация
# --------------------------------------------------------------------------- #

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN не задан в .env")

ADMIN_IDS: set[int] = {
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x
}
if not ADMIN_IDS:
    raise SystemExit("ADMIN_IDS не заданы в .env")

LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0") or 0)
if not LOG_CHANNEL_ID:
    raise SystemExit("LOG_CHANNEL_ID не задан в .env")

CRYPTO_PAY_TOKEN = os.getenv("CRYPTO_PAY_TOKEN", "").strip()
CRYPTO_PAY_TESTNET = os.getenv("CRYPTO_PAY_TESTNET", "false").lower() == "true"
CRYPTO_PAY_BASE = (
    "https://testnet-pay.crypt.bot/api" if CRYPTO_PAY_TESTNET else "https://pay.crypt.bot/api"
)


def _default_db_path() -> str:
    if os.path.isdir("/data"):
        return "/data/course.db"
    return "course.db"


DB_PATH = os.getenv("DB_PATH") or _default_db_path()

DEFAULT_REFERRAL_PERCENT = 10  # % продавцу за приведённого (меняется в админке)

SUPPORTED_CONTENT_TYPES: tuple[str, ...] = (
    "text",
    "photo",
    "video",
    "document",
    "audio",
    "voice",
    "animation",
    "video_note",
)

PAGE_SIZE = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("course-bot")

# --------------------------------------------------------------------------- #
# БД
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id       INTEGER PRIMARY KEY,
    username    TEXT,
    full_name   TEXT,
    referrer_id INTEGER,
    balance     INTEGER NOT NULL DEFAULT 0,    -- реф. баланс в копейках (для вывода)
    is_banned   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS courses (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    title            TEXT    NOT NULL,
    short_description TEXT   NOT NULL DEFAULT '',
    description      TEXT    NOT NULL DEFAULT '',
    price            INTEGER NOT NULL,                 -- в копейках
    cover_type       TEXT,                             -- photo/video/animation/null
    cover_file_id    TEXT,
    is_published     INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lessons (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id       INTEGER NOT NULL,
    position        INTEGER NOT NULL DEFAULT 0,
    title           TEXT    NOT NULL,
    description     TEXT    NOT NULL DEFAULT '',
    content_type    TEXT    NOT NULL,                  -- text/photo/video/...
    content_file_id TEXT,
    content_text    TEXT,
    content_caption TEXT,
    is_free         INTEGER NOT NULL DEFAULT 0,        -- пробный урок
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS lessons_course_idx ON lessons(course_id, position);

CREATE TABLE IF NOT EXISTS purchases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    course_id   INTEGER NOT NULL,
    price       INTEGER NOT NULL,                      -- фактически уплачено (после скидки) в коп.
    promo_code  TEXT,
    method      TEXT    NOT NULL,                      -- crypto/manual/admin
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS purchases_unique ON purchases(user_id, course_id);

CREATE TABLE IF NOT EXISTS promo_codes (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    code              TEXT    NOT NULL UNIQUE,
    discount_percent  INTEGER NOT NULL,                -- 1..100
    usage_limit       INTEGER,                         -- NULL = без лимита
    used_count        INTEGER NOT NULL DEFAULT 0,
    active            INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS deposits (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER NOT NULL,
    course_id         INTEGER NOT NULL,
    method            TEXT    NOT NULL,                -- crypto/manual
    amount            INTEGER NOT NULL,                -- к оплате в коп.
    promo_code        TEXT,
    status            TEXT    NOT NULL DEFAULT 'pending',   -- pending/paid/rejected
    invoice_id        TEXT,
    invoice_url       TEXT,
    moderation_msg_id INTEGER,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS withdraws (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    amount      INTEGER NOT NULL,
    card        TEXT    NOT NULL,
    fio         TEXT    NOT NULL,
    bank        TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'pending',
    moderation_msg_id INTEGER,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS referral_payouts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    referrer_id INTEGER NOT NULL,
    referee_id  INTEGER NOT NULL,
    course_id   INTEGER NOT NULL,
    amount      INTEGER NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.executescript(SCHEMA)
        await conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
            ("referral_percent", str(DEFAULT_REFERRAL_PERCENT)),
        )
        await conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
            ("requisites", ""),
        )
        await conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
            ("welcome_text", "👋 Добро пожаловать в школу маникюра!\n\nВыбирайте курс и начинайте учиться."),
        )
        await conn.commit()


async def setting_get(key: str, default: str = "") -> str:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        row = await (await conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        )).fetchone()
        return row["value"] if row else default


async def setting_set(key: str, value: str) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await conn.commit()


async def ensure_user(tg_id: int, username: Optional[str], full_name: str,
                      referrer_id: Optional[int] = None) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        existing = await (await conn.execute(
            "SELECT tg_id, referrer_id FROM users WHERE tg_id = ?", (tg_id,)
        )).fetchone()
        if existing is None:
            await conn.execute(
                "INSERT INTO users(tg_id, username, full_name, referrer_id) "
                "VALUES (?, ?, ?, ?)",
                (tg_id, username or "", full_name, referrer_id),
            )
        else:
            # обновляем имя/ник; реферер меняем только если ещё не задан и сам себе не реферер
            set_referrer = (
                existing["referrer_id"] is None and referrer_id and referrer_id != tg_id
            )
            if set_referrer:
                await conn.execute(
                    "UPDATE users SET username = ?, full_name = ?, referrer_id = ? "
                    "WHERE tg_id = ?",
                    (username or "", full_name, referrer_id, tg_id),
                )
            else:
                await conn.execute(
                    "UPDATE users SET username = ?, full_name = ? WHERE tg_id = ?",
                    (username or "", full_name, tg_id),
                )
        await conn.commit()


async def get_user(tg_id: int) -> Optional[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        return await (await conn.execute(
            "SELECT * FROM users WHERE tg_id = ?", (tg_id,)
        )).fetchone()


async def add_balance(tg_id: int, delta_kop: int) -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(
            "UPDATE users SET balance = balance + ? WHERE tg_id = ?",
            (delta_kop, tg_id),
        )
        row = await (await conn.execute(
            "SELECT balance FROM users WHERE tg_id = ?", (tg_id,)
        )).fetchone()
        await conn.commit()
        return row["balance"] if row else 0


async def has_course(user_id: int, course_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        row = await (await conn.execute(
            "SELECT 1 FROM purchases WHERE user_id = ? AND course_id = ?",
            (user_id, course_id),
        )).fetchone()
    return row is not None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def rub(kopecks: int) -> str:
    sign = "-" if kopecks < 0 else ""
    kopecks = abs(int(kopecks))
    rubles, kop = divmod(kopecks, 100)
    return f"{sign}{rubles:,}".replace(",", " ") + f",{kop:02d} ₽"


def parse_rub_to_kopecks(raw: str) -> Optional[int]:
    s = (raw or "").strip().replace(" ", "").replace(",", ".")
    try:
        val = float(s)
    except ValueError:
        return None
    if val < 0:
        return None
    return int(round(val * 100))


def is_admin(tg_id: int) -> bool:
    return tg_id in ADMIN_IDS


def esc(text: Any) -> str:
    return html.escape(str(text or ""), quote=False)


async def get_ref_percent() -> int:
    raw = await setting_get("referral_percent", str(DEFAULT_REFERRAL_PERCENT))
    try:
        return max(0, min(100, int(raw)))
    except ValueError:
        return DEFAULT_REFERRAL_PERCENT


def random_promo_code(n: int = 8) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(pysecrets.choice(alphabet) for _ in range(n))


# --------------------------------------------------------------------------- #
# Crypto Pay API
# --------------------------------------------------------------------------- #

class CryptoPay:
    def __init__(self, token: str, base_url: str) -> None:
        self.token = token
        self.base = base_url

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    async def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Crypto-Pay-API-Token": self.token}
        url = f"{self.base}/{method}"
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(url, json=payload, headers=headers) as resp:
                data = await resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"CryptoPay {method} error: {data}")
        return data["result"]

    async def create_invoice(self, amount_rub: float, description: str,
                             payload: str) -> dict[str, Any]:
        return await self._call(
            "createInvoice",
            {
                "currency_type": "fiat",
                "fiat": "RUB",
                "amount": f"{amount_rub:.2f}",
                "description": description[:1024],
                "payload": payload[:4096],
                "expires_in": 3600,
                "allow_comments": False,
                "allow_anonymous": True,
            },
        )

    async def get_invoices(self, invoice_ids: list[str]) -> list[dict[str, Any]]:
        if not invoice_ids:
            return []
        res = await self._call("getInvoices", {"invoice_ids": ",".join(invoice_ids)})
        return res.get("items", [])


crypto_pay = CryptoPay(CRYPTO_PAY_TOKEN, CRYPTO_PAY_BASE)

# --------------------------------------------------------------------------- #
# FSM
# --------------------------------------------------------------------------- #

class AdminCourseSG(StatesGroup):
    title = State()
    short_description = State()
    description = State()
    price = State()
    cover = State()


class AdminEditCourseSG(StatesGroup):
    field = State()
    value = State()


class AdminLessonSG(StatesGroup):
    title = State()
    description = State()
    is_free = State()
    content = State()


class AdminLessonEditSG(StatesGroup):
    field = State()
    value = State()


class AdminSG(StatesGroup):
    broadcast = State()
    set_requisites = State()
    set_welcome = State()
    set_ref_percent = State()
    give_user = State()
    give_amount = State()
    grant_user = State()
    promo_code = State()
    promo_discount = State()
    promo_limit = State()


class BuySG(StatesGroup):
    promo = State()
    manual_proof = State()


class WithdrawSG(StatesGroup):
    amount = State()
    card = State()
    fio = State()
    bank = State()


# --------------------------------------------------------------------------- #
# Клавиатуры
# --------------------------------------------------------------------------- #

def kb_main(is_admin_user: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📚 Каталог курсов", callback_data="catalog:0")
    b.button(text="🎓 Мои курсы", callback_data="my_courses")
    b.button(text="👤 Профиль", callback_data="profile")
    b.button(text="🎁 Реф. программа", callback_data="ref")
    if is_admin_user:
        b.button(text="🛠 Админ-панель", callback_data="admin:menu")
    b.adjust(2, 2, 1)
    return b.as_markup()


def kb_back(cb: str = "main") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Назад", callback_data=cb)
    return b.as_markup()


def kb_admin_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📊 Статистика", callback_data="admin:stats")
    b.button(text="📚 Курсы", callback_data="admin:courses:0")
    b.button(text="➕ Новый курс", callback_data="admin:course_new")
    b.button(text="🏷 Промокоды", callback_data="admin:promos")
    b.button(text="📣 Рассылка", callback_data="admin:broadcast")
    b.button(text="💳 Реквизиты", callback_data="admin:req")
    b.button(text="🎁 Реф. комиссия", callback_data="admin:ref_pct")
    b.button(text="👤 Выдать курс", callback_data="admin:grant")
    b.button(text="💵 Выдать баланс", callback_data="admin:give")
    b.button(text="📝 Приветствие", callback_data="admin:welcome")
    b.button(text="⬅️ В меню", callback_data="main")
    b.adjust(2, 2, 2, 2, 2, 1)
    return b.as_markup()


def kb_mod_deposit(did: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Подтвердить оплату", callback_data=f"mod:dep:approve:{did}")
    b.button(text="❌ Отклонить", callback_data=f"mod:dep:reject:{did}")
    b.adjust(1)
    return b.as_markup()


def kb_mod_withdraw(wid: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Выплачено", callback_data=f"mod:w:approve:{wid}")
    b.button(text="❌ Отклонить", callback_data=f"mod:w:reject:{wid}")
    b.adjust(1)
    return b.as_markup()


# --------------------------------------------------------------------------- #
# Bot
# --------------------------------------------------------------------------- #

bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


async def set_bot_commands() -> None:
    public = [BotCommand(command="start", description="Главное меню")]
    await bot.set_my_commands(public, scope=BotCommandScopeDefault())
    admin_cmds = [
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="admin", description="Админ-панель"),
    ]
    for aid in ADMIN_IDS:
        with suppress(TelegramAPIError):
            await bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=aid))


# --------------------------------------------------------------------------- #
# /start, меню
# --------------------------------------------------------------------------- #

@router.message(CommandStart(deep_link=True))
async def cmd_start_deep(m: Message, command: CommandObject, state: FSMContext) -> None:
    await state.clear()
    referrer_id: Optional[int] = None
    payload = (command.args or "").strip()
    if payload.startswith("ref_"):
        try:
            ref = int(payload[4:])
            if ref != m.from_user.id:
                referrer_id = ref
        except ValueError:
            pass
    await ensure_user(m.from_user.id, m.from_user.username, m.from_user.full_name, referrer_id)
    welcome = await setting_get("welcome_text", "Привет!")
    await m.answer(welcome, reply_markup=kb_main(is_admin(m.from_user.id)))


@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext) -> None:
    await state.clear()
    await ensure_user(m.from_user.id, m.from_user.username, m.from_user.full_name)
    welcome = await setting_get("welcome_text", "Привет!")
    await m.answer(welcome, reply_markup=kb_main(is_admin(m.from_user.id)))


@router.message(Command("admin"))
async def cmd_admin(m: Message, state: FSMContext) -> None:
    if not is_admin(m.from_user.id):
        return
    await state.clear()
    await m.answer("<b>🛠 Админ-панель</b>", reply_markup=kb_admin_menu())


@router.callback_query(F.data == "main")
async def cb_main(c: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    welcome = await setting_get("welcome_text", "Привет!")
    with suppress(TelegramAPIError):
        await c.message.edit_text(welcome, reply_markup=kb_main(is_admin(c.from_user.id)))
    await c.answer()


@router.callback_query(F.data == "noop")
async def cb_noop(c: CallbackQuery) -> None:
    await c.answer()


# --------------------------------------------------------------------------- #
# Профиль
# --------------------------------------------------------------------------- #

@router.callback_query(F.data == "profile")
async def cb_profile(c: CallbackQuery) -> None:
    u = await get_user(c.from_user.id)
    balance = u["balance"] if u else 0
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cnt = (await (await conn.execute(
            "SELECT COUNT(*) AS c FROM purchases WHERE user_id = ?",
            (c.from_user.id,),
        )).fetchone())["c"]
        refs = (await (await conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE referrer_id = ?",
            (c.from_user.id,),
        )).fetchone())["c"]
    text = (
        f"<b>👤 Профиль</b>\n\n"
        f"ID: <code>{c.from_user.id}</code>\n"
        f"Куплено курсов: <b>{cnt}</b>\n"
        f"Приглашено людей: <b>{refs}</b>\n"
        f"Реферальный баланс: <b>{esc(rub(balance))}</b>"
    )
    b = InlineKeyboardBuilder()
    if balance > 0:
        b.button(text="💸 Вывести на карту", callback_data="withdraw:start")
    b.button(text="⬅️ В меню", callback_data="main")
    b.adjust(1)
    with suppress(TelegramAPIError):
        await c.message.edit_text(text, reply_markup=b.as_markup())
    await c.answer()


# --------------------------------------------------------------------------- #
# Реферальная программа
# --------------------------------------------------------------------------- #

@router.callback_query(F.data == "ref")
async def cb_ref(c: CallbackQuery) -> None:
    me = await bot.me()
    pct = await get_ref_percent()
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        refs = (await (await conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE referrer_id = ?",
            (c.from_user.id,),
        )).fetchone())["c"]
        earned = (await (await conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS s FROM referral_payouts WHERE referrer_id = ?",
            (c.from_user.id,),
        )).fetchone())["s"]
    link = f"https://t.me/{me.username}?start=ref_{c.from_user.id}"
    text = (
        f"<b>🎁 Реферальная программа</b>\n\n"
        f"Вы получаете <b>{pct}%</b> от каждой покупки приведённого ученика.\n\n"
        f"Ваша ссылка:\n<code>{esc(link)}</code>\n\n"
        f"Приглашено: <b>{refs}</b>\n"
        f"Заработано всего: <b>{esc(rub(earned))}</b>"
    )
    await c.message.edit_text(text, reply_markup=kb_back("main"))
    await c.answer()


# --------------------------------------------------------------------------- #
# Каталог курсов
# --------------------------------------------------------------------------- #

@router.callback_query(F.data.startswith("catalog:"))
async def cb_catalog(c: CallbackQuery) -> None:
    page = int(c.data.split(":")[1])
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (await conn.execute(
            "SELECT id, title, price FROM courses "
            "WHERE is_published = 1 "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            (PAGE_SIZE, page * PAGE_SIZE),
        )).fetchall()
        total = (await (await conn.execute(
            "SELECT COUNT(*) AS c FROM courses WHERE is_published = 1"
        )).fetchone())["c"]
    if not rows and page == 0:
        await c.message.edit_text(
            "📭 Курсы пока не опубликованы. Заходите позже.",
            reply_markup=kb_back("main"),
        )
        await c.answer()
        return

    b = InlineKeyboardBuilder()
    for r in rows:
        b.button(
            text=f"{r['title'][:40]} — {rub(r['price'])}",
            callback_data=f"course:{r['id']}",
        )
    b.adjust(1)
    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="⬅️", callback_data=f"catalog:{page-1}")
    if (page + 1) * PAGE_SIZE < total:
        nav.button(text="➡️", callback_data=f"catalog:{page+1}")
    nav.adjust(2)
    b.attach(nav)
    back = InlineKeyboardBuilder()
    back.button(text="⬅️ В меню", callback_data="main")
    b.attach(back)
    await c.message.edit_text(
        f"<b>📚 Каталог курсов</b>\nВсего: <b>{total}</b>",
        reply_markup=b.as_markup(),
    )
    await c.answer()


async def _render_course(cid: int) -> Optional[tuple[str, aiosqlite.Row, list[aiosqlite.Row]]]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        course = await (await conn.execute(
            "SELECT * FROM courses WHERE id = ?", (cid,)
        )).fetchone()
        if not course:
            return None
        lessons = await (await conn.execute(
            "SELECT id, title, is_free, position FROM lessons "
            "WHERE course_id = ? ORDER BY position, id",
            (cid,),
        )).fetchall()
    free_cnt = sum(1 for l in lessons if l["is_free"])
    text = (
        f"<b>{esc(course['title'])}</b>\n\n"
        f"{esc(course['short_description'] or '')}\n\n"
        f"{esc(course['description'] or '')}\n\n"
        f"🎬 Уроков: <b>{len(lessons)}</b>"
        + (f" (из них бесплатных: <b>{free_cnt}</b>)" if free_cnt else "")
        + f"\n💰 Цена: <b>{esc(rub(course['price']))}</b>"
    )
    return text, course, list(lessons)


@router.callback_query(F.data.startswith("course:"))
async def cb_course_view(c: CallbackQuery) -> None:
    cid = int(c.data.split(":")[1])
    rendered = await _render_course(cid)
    if not rendered:
        await c.answer("Курс не найден", show_alert=True)
        return
    text, course, lessons = rendered
    owned = await has_course(c.from_user.id, cid)

    b = InlineKeyboardBuilder()
    # Уроки как кнопки
    for l in lessons:
        locked = not (owned or l["is_free"] or is_admin(c.from_user.id))
        label = ("🔒 " if locked else "▶️ ") + l["title"][:40]
        if l["is_free"] and not owned:
            label = "🆓 " + l["title"][:40]
        b.button(text=label, callback_data=f"lesson:{l['id']}")
    b.adjust(1)

    action = InlineKeyboardBuilder()
    if owned:
        action.button(text="✅ Курс куплен", callback_data="noop")
    else:
        action.button(text=f"🛒 Купить за {rub(course['price'])}", callback_data=f"buy:{cid}")
    action.button(text="⬅️ В каталог", callback_data="catalog:0")
    action.adjust(1)
    b.attach(action)

    # Если есть обложка — показываем; иначе текст
    cover_type = course["cover_type"]
    cover_fid = course["cover_file_id"]
    try:
        if cover_type and cover_fid:
            await c.message.delete()
            kw = {"chat_id": c.message.chat.id, "caption": text,
                  "reply_markup": b.as_markup()}
            if cover_type == "photo":
                await bot.send_photo(photo=cover_fid, **kw)
            elif cover_type == "video":
                await bot.send_video(video=cover_fid, **kw)
            elif cover_type == "animation":
                await bot.send_animation(animation=cover_fid, **kw)
            else:
                await bot.send_message(c.message.chat.id, text, reply_markup=b.as_markup())
        else:
            await c.message.edit_text(text, reply_markup=b.as_markup())
    except TelegramAPIError:
        await bot.send_message(c.message.chat.id, text, reply_markup=b.as_markup())
    await c.answer()


# --------------------------------------------------------------------------- #
# Мои курсы
# --------------------------------------------------------------------------- #

@router.callback_query(F.data == "my_courses")
async def cb_my_courses(c: CallbackQuery) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (await conn.execute(
            "SELECT c.id, c.title, c.price FROM purchases p "
            "JOIN courses c ON c.id = p.course_id "
            "WHERE p.user_id = ? ORDER BY p.created_at DESC",
            (c.from_user.id,),
        )).fetchall()
    if not rows:
        await c.message.edit_text(
            "🎓 Вы пока не купили ни одного курса.",
            reply_markup=kb_back("main"),
        )
        await c.answer()
        return
    b = InlineKeyboardBuilder()
    for r in rows:
        b.button(text=f"▶️ {r['title'][:40]}", callback_data=f"course:{r['id']}")
    b.button(text="⬅️ В меню", callback_data="main")
    b.adjust(1)
    await c.message.edit_text(
        f"<b>🎓 Мои курсы</b>\nКуплено: <b>{len(rows)}</b>",
        reply_markup=b.as_markup(),
    )
    await c.answer()


# --------------------------------------------------------------------------- #
# Просмотр урока
# --------------------------------------------------------------------------- #

@router.callback_query(F.data.startswith("lesson:"))
async def cb_lesson(c: CallbackQuery) -> None:
    lid = int(c.data.split(":")[1])
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        lesson = await (await conn.execute(
            "SELECT * FROM lessons WHERE id = ?", (lid,)
        )).fetchone()
    if not lesson:
        await c.answer("Урок не найден", show_alert=True)
        return
    course_id = lesson["course_id"]
    owned = await has_course(c.from_user.id, course_id)
    if not (owned or lesson["is_free"] or is_admin(c.from_user.id)):
        await c.answer("🔒 Урок доступен после покупки курса", show_alert=True)
        return

    # Посылаем урок отдельным сообщением
    header = (
        f"<b>{esc(lesson['title'])}</b>\n\n"
        + (esc(lesson["description"]) + "\n\n" if lesson["description"] else "")
    )
    ct = lesson["content_type"]
    fid = lesson["content_file_id"]
    caption = lesson["content_caption"] or ""
    combined_caption = (header + caption).strip()[:1024]

    b = InlineKeyboardBuilder()
    b.button(text="⬅️ К курсу", callback_data=f"course:{course_id}")

    try:
        if ct == "text":
            await bot.send_message(
                c.message.chat.id,
                header + esc(lesson["content_text"] or ""),
                reply_markup=b.as_markup(),
            )
        elif ct == "photo":
            await bot.send_photo(
                c.message.chat.id, fid, caption=combined_caption, reply_markup=b.as_markup(),
            )
        elif ct == "video":
            await bot.send_video(
                c.message.chat.id, fid, caption=combined_caption, reply_markup=b.as_markup(),
            )
        elif ct == "document":
            await bot.send_document(
                c.message.chat.id, fid, caption=combined_caption, reply_markup=b.as_markup(),
            )
        elif ct == "audio":
            await bot.send_audio(
                c.message.chat.id, fid, caption=combined_caption, reply_markup=b.as_markup(),
            )
        elif ct == "voice":
            await bot.send_message(c.message.chat.id, header)
            await bot.send_voice(c.message.chat.id, fid, reply_markup=b.as_markup())
        elif ct == "animation":
            await bot.send_animation(
                c.message.chat.id, fid, caption=combined_caption, reply_markup=b.as_markup(),
            )
        elif ct == "video_note":
            await bot.send_message(c.message.chat.id, header)
            await bot.send_video_note(c.message.chat.id, fid, reply_markup=b.as_markup())
    except TelegramAPIError as e:
        log.warning("lesson send failed: %s", e)
        await c.answer("Не удалось отправить урок", show_alert=True)
        return

    await c.answer()


# --------------------------------------------------------------------------- #
# Покупка курса
# --------------------------------------------------------------------------- #

async def _compute_price(course_price: int, promo_code: Optional[str]
                         ) -> tuple[int, Optional[aiosqlite.Row]]:
    """Возвращает (цена_после_скидки, строка_промо или None). Если код плох — без скидки."""
    if not promo_code:
        return course_price, None
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        p = await (await conn.execute(
            "SELECT * FROM promo_codes WHERE code = ? AND active = 1",
            (promo_code.upper(),),
        )).fetchone()
    if not p:
        return course_price, None
    if p["usage_limit"] is not None and p["used_count"] >= p["usage_limit"]:
        return course_price, None
    discount = course_price * int(p["discount_percent"]) // 100
    return max(0, course_price - discount), p


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(c: CallbackQuery, state: FSMContext) -> None:
    cid = int(c.data.split(":")[1])
    rendered = await _render_course(cid)
    if not rendered:
        await c.answer("Курс не найден", show_alert=True)
        return
    _, course, _ = rendered
    if await has_course(c.from_user.id, cid):
        await c.answer("Вы уже купили этот курс", show_alert=True)
        return
    await state.update_data(buy_course_id=cid)
    text = (
        f"<b>Оформление курса «{esc(course['title'])}»</b>\n\n"
        f"Сумма: <b>{esc(rub(course['price']))}</b>\n\n"
        f"Выберите действие:"
    )
    b = InlineKeyboardBuilder()
    b.button(text="🏷 Применить промокод", callback_data=f"buy:promo:{cid}")
    if crypto_pay.enabled:
        b.button(text="🪙 Оплатить CryptoBot", callback_data=f"buy:crypto:{cid}")
    b.button(text="💳 Оплатить переводом на карту", callback_data=f"buy:manual:{cid}")
    b.button(text="⬅️ Назад", callback_data=f"course:{cid}")
    b.adjust(1)
    with suppress(TelegramAPIError):
        await c.message.edit_text(text, reply_markup=b.as_markup())
    # если сообщение было media — edit_text упадёт; шлём новое
    if c.message.text is None:
        await bot.send_message(c.message.chat.id, text, reply_markup=b.as_markup())
    await c.answer()


@router.callback_query(F.data.startswith("buy:promo:"))
async def cb_buy_promo(c: CallbackQuery, state: FSMContext) -> None:
    cid = int(c.data.split(":")[2])
    await state.update_data(buy_course_id=cid)
    await state.set_state(BuySG.promo)
    await bot.send_message(
        c.message.chat.id, "Введите промокод:",
        reply_markup=kb_back(f"course:{cid}"),
    )
    await c.answer()


@router.message(BuySG.promo)
async def buy_promo_apply(m: Message, state: FSMContext) -> None:
    data = await state.get_data()
    cid = int(data.get("buy_course_id", 0))
    if not cid:
        await state.clear()
        return
    code = (m.text or "").strip().upper()
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        course = await (await conn.execute(
            "SELECT * FROM courses WHERE id = ?", (cid,)
        )).fetchone()
    if not course:
        await state.clear()
        return
    price_after, promo = await _compute_price(course["price"], code)
    if promo is None:
        await m.answer("❌ Промокод не найден или недействителен.")
        await state.clear()
        await cmd_course_after_promo(m, cid, None, course["price"])
        return
    await state.update_data(promo_code=code, price_after=price_after)
    await state.clear()
    await m.answer(
        f"✅ Промокод применён: скидка <b>{promo['discount_percent']}%</b>.\n"
        f"Итого к оплате: <b>{esc(rub(price_after))}</b>"
    )
    await cmd_course_after_promo(m, cid, code, price_after)


async def cmd_course_after_promo(m: Message, cid: int, promo: Optional[str],
                                 price: int) -> None:
    b = InlineKeyboardBuilder()
    if crypto_pay.enabled:
        b.button(text=f"🪙 CryptoBot — {rub(price)}", callback_data=f"buy:crypto:{cid}")
    b.button(text=f"💳 Перевод на карту — {rub(price)}", callback_data=f"buy:manual:{cid}")
    b.button(text="⬅️ К курсу", callback_data=f"course:{cid}")
    b.adjust(1)
    await m.answer("Выберите способ оплаты:", reply_markup=b.as_markup())


async def _get_buy_context(user_id: int, cid: int, state: FSMContext
                           ) -> Optional[tuple[aiosqlite.Row, int, Optional[str]]]:
    data = await state.get_data()
    promo: Optional[str] = data.get("promo_code")
    price_after: Optional[int] = data.get("price_after")
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        course = await (await conn.execute(
            "SELECT * FROM courses WHERE id = ?", (cid,)
        )).fetchone()
    if not course:
        return None
    if price_after is None or data.get("buy_course_id") != cid:
        price_after, _ = await _compute_price(course["price"], promo)
    return course, int(price_after), promo


@router.callback_query(F.data.startswith("buy:crypto:"))
async def cb_buy_crypto(c: CallbackQuery, state: FSMContext) -> None:
    cid = int(c.data.split(":")[2])
    ctx = await _get_buy_context(c.from_user.id, cid, state)
    if not ctx:
        await c.answer("Курс не найден", show_alert=True)
        return
    course, price, promo = ctx
    if not crypto_pay.enabled:
        await c.answer("CryptoBot не настроен", show_alert=True)
        return
    try:
        inv = await crypto_pay.create_invoice(
            amount_rub=price / 100,
            description=f"Оплата курса «{course['title'][:80]}»",
            payload=f"user={c.from_user.id};course={cid}",
        )
    except Exception as e:
        log.exception("createInvoice: %s", e)
        await c.answer("Ошибка создания счёта", show_alert=True)
        return

    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "INSERT INTO deposits(user_id, course_id, method, amount, promo_code, "
            "                     status, invoice_id, invoice_url) "
            "VALUES (?, ?, 'crypto', ?, ?, 'pending', ?, ?)",
            (c.from_user.id, cid, price, promo, str(inv["invoice_id"]),
             inv.get("bot_invoice_url") or inv.get("pay_url")),
        )
        did = cur.lastrowid
        await conn.commit()

    b = InlineKeyboardBuilder()
    b.button(text="💳 Оплатить", url=inv.get("bot_invoice_url") or inv.get("pay_url"))
    b.button(text="🔄 Проверить оплату", callback_data=f"buy:check:{did}")
    b.button(text="⬅️ К курсу", callback_data=f"course:{cid}")
    b.adjust(1)
    await bot.send_message(
        c.message.chat.id,
        f"Счёт на <b>{esc(rub(price))}</b> создан.\n"
        "Оплатите через CryptoBot и нажмите «Проверить оплату».",
        reply_markup=b.as_markup(),
    )
    await c.answer()


@router.callback_query(F.data.startswith("buy:check:"))
async def cb_buy_check(c: CallbackQuery) -> None:
    did = int(c.data.split(":")[2])
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        dep = await (await conn.execute(
            "SELECT * FROM deposits WHERE id = ?", (did,)
        )).fetchone()
    if not dep or dep["user_id"] != c.from_user.id:
        await c.answer("Не найдено", show_alert=True)
        return
    if dep["status"] == "paid":
        await c.answer("Уже оплачено", show_alert=True)
        return
    if not dep["invoice_id"]:
        await c.answer("Нет invoice", show_alert=True)
        return
    try:
        items = await crypto_pay.get_invoices([dep["invoice_id"]])
    except Exception as e:
        log.exception("getInvoices: %s", e)
        await c.answer("Ошибка запроса", show_alert=True)
        return
    if not items or items[0].get("status") != "paid":
        await c.answer("Оплата ещё не поступила", show_alert=True)
        return
    await _finalize_purchase(did)
    await c.message.edit_text(
        "✅ Оплата получена! Курс открыт в разделе «Мои курсы».",
        reply_markup=kb_back("main"),
    )
    await c.answer("Оплачено!")


@router.callback_query(F.data.startswith("buy:manual:"))
async def cb_buy_manual(c: CallbackQuery, state: FSMContext) -> None:
    cid = int(c.data.split(":")[2])
    ctx = await _get_buy_context(c.from_user.id, cid, state)
    if not ctx:
        await c.answer("Курс не найден", show_alert=True)
        return
    course, price, promo = ctx
    req = await setting_get("requisites", "")
    if not req.strip():
        await c.answer("Реквизиты ещё не заданы админом", show_alert=True)
        return
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "INSERT INTO deposits(user_id, course_id, method, amount, promo_code, status) "
            "VALUES (?, ?, 'manual', ?, ?, 'pending')",
            (c.from_user.id, cid, price, promo),
        )
        did = cur.lastrowid
        await conn.commit()
    await state.update_data(pending_deposit_id=did)
    await state.set_state(BuySG.manual_proof)
    await bot.send_message(
        c.message.chat.id,
        f"<b>Оплата курса «{esc(course['title'])}»</b>\n\n"
        f"Переведите <b>{esc(rub(price))}</b> по реквизитам:\n"
        f"<blockquote>{esc(req)}</blockquote>\n"
        f"После перевода пришлите скрин/файл подтверждения одним сообщением.",
        reply_markup=kb_back(f"course:{cid}"),
    )
    await c.answer()


@router.message(BuySG.manual_proof)
async def buy_manual_proof(m: Message, state: FSMContext) -> None:
    data = await state.get_data()
    did = int(data.get("pending_deposit_id", 0))
    if not did:
        await state.clear()
        return
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        dep = await (await conn.execute(
            "SELECT * FROM deposits WHERE id = ?", (did,)
        )).fetchone()
    if not dep or dep["user_id"] != m.from_user.id or dep["status"] != "pending":
        await state.clear()
        return
    course = await _get_course(int(dep["course_id"]))
    caption = (
        f"💳 <b>Оплата курса #{dep['course_id']} «{esc(course['title'] if course else '?')}»</b>\n"
        f"Заявка: <b>#{did}</b>\n"
        f"Юзер: <a href=\"tg://user?id={m.from_user.id}\">{esc(m.from_user.full_name)}</a>"
        f" (<code>{m.from_user.id}</code>)\n"
        f"Сумма: <b>{esc(rub(int(dep['amount'])))}</b>"
        + (f"\nПромокод: <code>{esc(dep['promo_code'])}</code>" if dep["promo_code"] else "")
    )
    try:
        fwd = await m.forward(LOG_CHANNEL_ID)
        msg = await bot.send_message(
            LOG_CHANNEL_ID, caption,
            reply_markup=kb_mod_deposit(did),
            reply_parameters=ReplyParameters(message_id=fwd.message_id),
        )
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                "UPDATE deposits SET moderation_msg_id = ? WHERE id = ?",
                (msg.message_id, did),
            )
            await conn.commit()
    except TelegramAPIError as e:
        log.exception("manual proof notify failed: %s", e)

    await state.clear()
    await m.answer(
        "📨 Заявка отправлена администратору. После подтверждения курс откроется.",
        reply_markup=kb_main(is_admin(m.from_user.id)),
    )


async def _get_course(cid: int) -> Optional[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        return await (await conn.execute(
            "SELECT * FROM courses WHERE id = ?", (cid,)
        )).fetchone()


async def _finalize_purchase(deposit_id: int) -> None:
    """Отмечает депозит оплаченным, создаёт purchase и начисляет реф. бонус."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        dep = await (await conn.execute(
            "SELECT * FROM deposits WHERE id = ?", (deposit_id,)
        )).fetchone()
        if not dep or dep["status"] == "paid":
            return
        await conn.execute(
            "UPDATE deposits SET status = 'paid' WHERE id = ?", (deposit_id,)
        )
        # purchase (на случай повторной покупки — игнорируем)
        await conn.execute(
            "INSERT OR IGNORE INTO purchases(user_id, course_id, price, promo_code, method) "
            "VALUES (?, ?, ?, ?, ?)",
            (dep["user_id"], dep["course_id"], dep["amount"],
             dep["promo_code"], dep["method"]),
        )
        # увеличить used_count промокода
        if dep["promo_code"]:
            await conn.execute(
                "UPDATE promo_codes SET used_count = used_count + 1 "
                "WHERE code = ?",
                (dep["promo_code"],),
            )
        # ищем реферера
        user_row = await (await conn.execute(
            "SELECT referrer_id FROM users WHERE tg_id = ?", (dep["user_id"],)
        )).fetchone()
        await conn.commit()

    if user_row and user_row["referrer_id"]:
        pct = await get_ref_percent()
        bonus = int(dep["amount"]) * pct // 100
        if bonus > 0:
            await add_balance(int(user_row["referrer_id"]), bonus)
            async with aiosqlite.connect(DB_PATH) as conn:
                await conn.execute(
                    "INSERT INTO referral_payouts(referrer_id, referee_id, course_id, amount) "
                    "VALUES (?, ?, ?, ?)",
                    (int(user_row["referrer_id"]), int(dep["user_id"]),
                     int(dep["course_id"]), bonus),
                )
                await conn.commit()
            with suppress(TelegramAPIError):
                await bot.send_message(
                    int(user_row["referrer_id"]),
                    f"🎁 Вам начислен реферальный бонус <b>{esc(rub(bonus))}</b> "
                    f"за покупку вашего приглашённого.",
                )

    with suppress(TelegramAPIError):
        course = await _get_course(int(dep["course_id"]))
        await bot.send_message(
            int(dep["user_id"]),
            f"✅ Оплата подтверждена! Курс <b>{esc(course['title'] if course else '')}</b> "
            f"открыт.",
            reply_markup=kb_back("main"),
        )


# --------------------------------------------------------------------------- #
# Модерация депозитов (ручная оплата)
# --------------------------------------------------------------------------- #

@router.callback_query(F.data.startswith("mod:dep:"))
async def cb_mod_dep(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        await c.answer("Нет прав", show_alert=True)
        return
    _, _, action, did_raw = c.data.split(":")
    did = int(did_raw)
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        dep = await (await conn.execute(
            "SELECT * FROM deposits WHERE id = ?", (did,)
        )).fetchone()
    if not dep or dep["status"] != "pending":
        await c.answer("Уже обработано", show_alert=True)
        return
    if action == "approve":
        await _finalize_purchase(did)
        suffix = "\n\n✅ Подтверждено"
    else:
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                "UPDATE deposits SET status = 'rejected' WHERE id = ?", (did,)
            )
            await conn.commit()
        with suppress(TelegramAPIError):
            await bot.send_message(
                int(dep["user_id"]),
                f"❌ Оплата по заявке #{did} отклонена.",
            )
        suffix = "\n\n❌ Отклонено"
    with suppress(TelegramAPIError):
        await c.message.edit_text(
            (c.message.html_text or c.message.text or "") + suffix,
            reply_markup=None,
        )
    await c.answer("Готово")


# --------------------------------------------------------------------------- #
# Вывод реф. баланса на карту
# --------------------------------------------------------------------------- #

@router.callback_query(F.data == "withdraw:start")
async def cb_wd_start(c: CallbackQuery, state: FSMContext) -> None:
    u = await get_user(c.from_user.id)
    if not u or u["balance"] <= 0:
        await c.answer("Пустой баланс", show_alert=True)
        return
    await state.set_state(WithdrawSG.amount)
    await c.message.edit_text(
        f"<b>💸 Вывод на карту</b>\n\n"
        f"Доступно: <b>{esc(rub(u['balance']))}</b>\n\n"
        f"Введите сумму вывода в рублях:",
        reply_markup=kb_back("profile"),
    )
    await c.answer()


@router.message(WithdrawSG.amount)
async def wd_amount(m: Message, state: FSMContext) -> None:
    kop = parse_rub_to_kopecks(m.text or "")
    if kop is None or kop < 100:
        await m.answer("Минимум 1 ₽. Повторите.")
        return
    u = await get_user(m.from_user.id)
    if not u or u["balance"] < kop:
        await m.answer("Недостаточно на балансе.")
        return
    await state.update_data(wd_amount=kop)
    await state.set_state(WithdrawSG.card)
    await m.answer("Введите <b>номер карты</b>:")


@router.message(WithdrawSG.card)
async def wd_card(m: Message, state: FSMContext) -> None:
    card = (m.text or "").strip()
    digits = "".join(ch for ch in card if ch.isdigit())
    if not (13 <= len(digits) <= 19):
        await m.answer("Похоже, это не номер карты. Повторите.")
        return
    await state.update_data(wd_card=card)
    await state.set_state(WithdrawSG.fio)
    await m.answer("Введите <b>ФИО получателя</b>:")


@router.message(WithdrawSG.fio)
async def wd_fio(m: Message, state: FSMContext) -> None:
    fio = (m.text or "").strip()
    if len(fio) < 3:
        await m.answer("ФИО слишком короткое.")
        return
    await state.update_data(wd_fio=fio)
    await state.set_state(WithdrawSG.bank)
    await m.answer("Введите <b>банк</b>:")


@router.message(WithdrawSG.bank)
async def wd_bank(m: Message, state: FSMContext) -> None:
    bank = (m.text or "").strip()
    if not bank:
        await m.answer("Нужно указать банк.")
        return
    data = await state.get_data()
    amount = int(data["wd_amount"])
    card = data["wd_card"]
    fio = data["wd_fio"]

    u = await get_user(m.from_user.id)
    if not u or u["balance"] < amount:
        await m.answer("Баланс изменился, попробуйте ещё раз.")
        await state.clear()
        return

    await add_balance(m.from_user.id, -amount)

    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "INSERT INTO withdraws(user_id, amount, card, fio, bank) "
            "VALUES (?, ?, ?, ?, ?)",
            (m.from_user.id, amount, card, fio, bank),
        )
        wid = cur.lastrowid
        await conn.commit()

    text = (
        f"💸 <b>Заявка на вывод #{wid}</b>\n"
        f"Юзер: <a href=\"tg://user?id={m.from_user.id}\">{esc(m.from_user.full_name)}</a>"
        f" (<code>{m.from_user.id}</code>)\n"
        f"Сумма: <b>{esc(rub(amount))}</b>\n\n"
        f"Карта: <code>{esc(card)}</code>\n"
        f"ФИО: <b>{esc(fio)}</b>\n"
        f"Банк: <b>{esc(bank)}</b>"
    )
    with suppress(TelegramAPIError):
        msg = await bot.send_message(LOG_CHANNEL_ID, text, reply_markup=kb_mod_withdraw(wid))
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                "UPDATE withdraws SET moderation_msg_id = ? WHERE id = ?",
                (msg.message_id, wid),
            )
            await conn.commit()

    await state.clear()
    await m.answer(
        f"✅ Заявка #{wid} создана, ожидайте выплаты.",
        reply_markup=kb_main(is_admin(m.from_user.id)),
    )


@router.callback_query(F.data.startswith("mod:w:"))
async def cb_mod_w(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        await c.answer("Нет прав", show_alert=True)
        return
    _, _, action, wid_raw = c.data.split(":")
    wid = int(wid_raw)
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        w = await (await conn.execute(
            "SELECT * FROM withdraws WHERE id = ?", (wid,)
        )).fetchone()
    if not w or w["status"] != "pending":
        await c.answer("Уже обработано", show_alert=True)
        return
    if action == "approve":
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                "UPDATE withdraws SET status = 'paid' WHERE id = ?", (wid,)
            )
            await conn.commit()
        with suppress(TelegramAPIError):
            await bot.send_message(
                int(w["user_id"]),
                f"✅ Выплата #{wid} на <b>{esc(rub(int(w['amount'])))}</b> отправлена.",
            )
        suffix = "\n\n✅ Выплачено"
    else:
        await add_balance(int(w["user_id"]), int(w["amount"]))
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                "UPDATE withdraws SET status = 'rejected' WHERE id = ?", (wid,)
            )
            await conn.commit()
        with suppress(TelegramAPIError):
            await bot.send_message(
                int(w["user_id"]),
                f"❌ Заявка #{wid} отклонена, баланс возвращён.",
            )
        suffix = "\n\n❌ Отклонено"
    with suppress(TelegramAPIError):
        await c.message.edit_text(
            (c.message.html_text or c.message.text or "") + suffix,
            reply_markup=None,
        )
    await c.answer("Готово")


# --------------------------------------------------------------------------- #
# Админ-панель
# --------------------------------------------------------------------------- #

@router.callback_query(F.data == "admin:menu")
async def cb_admin_menu(c: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(c.from_user.id):
        return
    await state.clear()
    await c.message.edit_text("<b>🛠 Админ-панель</b>", reply_markup=kb_admin_menu())
    await c.answer()


# --- Статистика --- #

@router.callback_query(F.data == "admin:stats")
async def cb_admin_stats(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        users = (await (await conn.execute(
            "SELECT COUNT(*) AS c FROM users"
        )).fetchone())["c"]
        courses = (await (await conn.execute(
            "SELECT COUNT(*) AS c FROM courses"
        )).fetchone())["c"]
        published = (await (await conn.execute(
            "SELECT COUNT(*) AS c FROM courses WHERE is_published = 1"
        )).fetchone())["c"]
        sales = await (await conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(price), 0) AS s FROM purchases"
        )).fetchone()
        pending_deps = (await (await conn.execute(
            "SELECT COUNT(*) AS c FROM deposits WHERE status = 'pending'"
        )).fetchone())["c"]
        pending_wds = (await (await conn.execute(
            "SELECT COUNT(*) AS c FROM withdraws WHERE status = 'pending'"
        )).fetchone())["c"]
    pct = await get_ref_percent()
    text = (
        "<b>📊 Статистика</b>\n\n"
        f"Пользователей: <b>{users}</b>\n"
        f"Курсов всего: <b>{courses}</b> (опубликовано: {published})\n"
        f"Продаж: <b>{sales['c']}</b> на <b>{esc(rub(sales['s']))}</b>\n"
        f"Оплат ожидают: <b>{pending_deps}</b>\n"
        f"Выплат ожидают: <b>{pending_wds}</b>\n"
        f"Реф. комиссия: <b>{pct}%</b>"
    )
    await c.message.edit_text(text, reply_markup=kb_back("admin:menu"))
    await c.answer()


# --- Рассылка --- #

@router.callback_query(F.data == "admin:broadcast")
async def cb_admin_broadcast(c: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(c.from_user.id):
        return
    await state.set_state(AdminSG.broadcast)
    await c.message.edit_text(
        "📣 Пришлите сообщение для рассылки (текст/медиа). Оно будет отправлено как копия всем пользователям.",
        reply_markup=kb_back("admin:menu"),
    )
    await c.answer()


@router.message(AdminSG.broadcast)
async def admin_broadcast(m: Message, state: FSMContext) -> None:
    if not is_admin(m.from_user.id):
        return
    await state.clear()
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (await conn.execute("SELECT tg_id FROM users")).fetchall()
    await m.answer(f"Начинаю рассылку по {len(rows)} пользователям…")
    sent, blocked, failed = 0, 0, 0
    for r in rows:
        try:
            await bot.copy_message(r["tg_id"], m.chat.id, m.message_id)
            sent += 1
        except TelegramForbiddenError:
            blocked += 1
        except TelegramAPIError:
            failed += 1
        await asyncio.sleep(0.04)
    await m.answer(
        f"Готово. Доставлено: <b>{sent}</b>, заблокировали бот: <b>{blocked}</b>, "
        f"ошибок: <b>{failed}</b>.",
        reply_markup=kb_admin_menu(),
    )


# --- Реквизиты и приветствие --- #

@router.callback_query(F.data == "admin:req")
async def cb_admin_req(c: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(c.from_user.id):
        return
    current = await setting_get("requisites", "(не задано)")
    await state.set_state(AdminSG.set_requisites)
    await c.message.edit_text(
        f"<b>💳 Реквизиты</b>\n\n"
        f"Текущие:\n<blockquote>{esc(current)}</blockquote>\n\n"
        f"Пришлите новые одним сообщением:",
        reply_markup=kb_back("admin:menu"),
    )
    await c.answer()


@router.message(AdminSG.set_requisites)
async def admin_set_req(m: Message, state: FSMContext) -> None:
    text = (m.text or "").strip()
    if not text:
        return
    await setting_set("requisites", text)
    await state.clear()
    await m.answer("✅ Реквизиты обновлены.", reply_markup=kb_admin_menu())


@router.callback_query(F.data == "admin:welcome")
async def cb_admin_welcome(c: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(c.from_user.id):
        return
    current = await setting_get("welcome_text", "")
    await state.set_state(AdminSG.set_welcome)
    await c.message.edit_text(
        f"<b>📝 Приветственное сообщение</b>\n\n"
        f"Текущее:\n<blockquote>{esc(current)[:500]}</blockquote>\n\n"
        f"Пришлите новое (HTML поддерживается):",
        reply_markup=kb_back("admin:menu"),
    )
    await c.answer()


@router.message(AdminSG.set_welcome)
async def admin_set_welcome(m: Message, state: FSMContext) -> None:
    text = (m.text or m.html_text or "").strip()
    if not text:
        return
    await setting_set("welcome_text", text)
    await state.clear()
    await m.answer("✅ Приветствие обновлено.", reply_markup=kb_admin_menu())


# --- Реф. комиссия --- #

@router.callback_query(F.data == "admin:ref_pct")
async def cb_admin_ref_pct(c: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(c.from_user.id):
        return
    pct = await get_ref_percent()
    await state.set_state(AdminSG.set_ref_percent)
    await c.message.edit_text(
        f"🎁 Текущий % рефералки: <b>{pct}%</b>\n\nВведите новое значение (0–100):",
        reply_markup=kb_back("admin:menu"),
    )
    await c.answer()


@router.message(AdminSG.set_ref_percent)
async def admin_set_ref_pct(m: Message, state: FSMContext) -> None:
    try:
        v = int((m.text or "").strip())
    except ValueError:
        await m.answer("Нужно целое 0..100")
        return
    if not 0 <= v <= 100:
        await m.answer("Диапазон 0..100")
        return
    await setting_set("referral_percent", str(v))
    await state.clear()
    await m.answer(f"✅ Реф. комиссия: <b>{v}%</b>", reply_markup=kb_admin_menu())


# --- Выдать баланс --- #

@router.callback_query(F.data == "admin:give")
async def cb_admin_give(c: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(c.from_user.id):
        return
    await state.set_state(AdminSG.give_user)
    await c.message.edit_text(
        "💵 Введите <b>tg_id</b> пользователя:",
        reply_markup=kb_back("admin:menu"),
    )
    await c.answer()


@router.message(AdminSG.give_user)
async def admin_give_user(m: Message, state: FSMContext) -> None:
    try:
        uid = int((m.text or "").strip())
    except ValueError:
        await m.answer("Нужно число")
        return
    await state.update_data(give_uid=uid)
    await state.set_state(AdminSG.give_amount)
    await m.answer("Сумма в рублях (может быть отрицательной):")


@router.message(AdminSG.give_amount)
async def admin_give_amount(m: Message, state: FSMContext) -> None:
    raw = (m.text or "").strip().replace(" ", "").replace(",", ".")
    try:
        val = float(raw)
    except ValueError:
        await m.answer("Некорректная сумма")
        return
    kop = int(round(val * 100))
    data = await state.get_data()
    uid = int(data["give_uid"])
    # гарантируем наличие юзера
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO users(tg_id, username, full_name) VALUES (?, '', '')",
            (uid,),
        )
        await conn.commit()
    new_bal = await add_balance(uid, kop)
    with suppress(TelegramAPIError):
        await bot.send_message(
            uid,
            f"💵 Администратор изменил ваш реф. баланс на <b>{esc(rub(kop))}</b>. "
            f"Итого: <b>{esc(rub(new_bal))}</b>.",
        )
    await state.clear()
    await m.answer(
        f"Готово. Баланс {uid}: <b>{esc(rub(new_bal))}</b>",
        reply_markup=kb_admin_menu(),
    )


# --- Выдать курс --- #

@router.callback_query(F.data == "admin:grant")
async def cb_admin_grant(c: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(c.from_user.id):
        return
    await state.set_state(AdminSG.grant_user)
    await c.message.edit_text(
        "🎁 Выдача курса вручную. Введите <b>tg_id юзера,id_курса</b> через запятую:\n"
        "Пример: <code>123456789,2</code>",
        reply_markup=kb_back("admin:menu"),
    )
    await c.answer()


@router.message(AdminSG.grant_user)
async def admin_grant_do(m: Message, state: FSMContext) -> None:
    try:
        parts = [p.strip() for p in (m.text or "").split(",")]
        uid = int(parts[0])
        cid = int(parts[1])
    except (ValueError, IndexError):
        await m.answer("Формат: <code>tg_id,course_id</code>")
        return
    course = await _get_course(cid)
    if not course:
        await m.answer("Курс не найден")
        return
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO users(tg_id, username, full_name) VALUES (?, '', '')",
            (uid,),
        )
        await conn.execute(
            "INSERT OR IGNORE INTO purchases(user_id, course_id, price, method) "
            "VALUES (?, ?, 0, 'admin')",
            (uid, cid),
        )
        await conn.commit()
    with suppress(TelegramAPIError):
        await bot.send_message(
            uid,
            f"🎁 Вам выдан доступ к курсу <b>{esc(course['title'])}</b>.",
        )
    await state.clear()
    await m.answer(
        f"✅ Курс #{cid} выдан пользователю {uid}.",
        reply_markup=kb_admin_menu(),
    )


# --- Курсы: список --- #

@router.callback_query(F.data.startswith("admin:courses:"))
async def cb_admin_courses(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    page = int(c.data.split(":")[2])
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (await conn.execute(
            "SELECT id, title, price, is_published FROM courses "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            (PAGE_SIZE, page * PAGE_SIZE),
        )).fetchall()
        total = (await (await conn.execute(
            "SELECT COUNT(*) AS c FROM courses"
        )).fetchone())["c"]
    b = InlineKeyboardBuilder()
    for r in rows:
        emoji = "🟢" if r["is_published"] else "⚪️"
        b.button(
            text=f"{emoji} #{r['id']} {r['title'][:30]} — {rub(r['price'])}",
            callback_data=f"admin:course:{r['id']}",
        )
    b.adjust(1)
    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="⬅️", callback_data=f"admin:courses:{page-1}")
    if (page + 1) * PAGE_SIZE < total:
        nav.button(text="➡️", callback_data=f"admin:courses:{page+1}")
    nav.adjust(2)
    b.attach(nav)
    tail = InlineKeyboardBuilder()
    tail.button(text="➕ Новый курс", callback_data="admin:course_new")
    tail.button(text="⬅️ Админ", callback_data="admin:menu")
    tail.adjust(1)
    b.attach(tail)
    await c.message.edit_text(
        f"<b>📚 Курсы ({total})</b>", reply_markup=b.as_markup()
    )
    await c.answer()


# --- Добавление курса (FSM) --- #

@router.callback_query(F.data == "admin:course_new")
async def cb_admin_course_new(c: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(c.from_user.id):
        return
    await state.set_state(AdminCourseSG.title)
    await c.message.edit_text(
        "<b>Новый курс — 1/5</b>\n\nВведите название:",
        reply_markup=kb_back("admin:menu"),
    )
    await c.answer()


@router.message(AdminCourseSG.title)
async def cnew_title(m: Message, state: FSMContext) -> None:
    t = (m.text or "").strip()
    if not (1 <= len(t) <= 100):
        await m.answer("1..100 символов")
        return
    await state.update_data(title=t)
    await state.set_state(AdminCourseSG.short_description)
    await m.answer("<b>2/5</b>\n\nКраткое описание (1 строка):")


@router.message(AdminCourseSG.short_description)
async def cnew_short(m: Message, state: FSMContext) -> None:
    t = (m.text or "").strip()[:200]
    await state.update_data(short_description=t)
    await state.set_state(AdminCourseSG.description)
    await m.answer("<b>3/5</b>\n\nПодробное описание (до 2000 симв., HTML поддерживается):")


@router.message(AdminCourseSG.description)
async def cnew_desc(m: Message, state: FSMContext) -> None:
    t = (m.text or m.html_text or "").strip()[:2000]
    await state.update_data(description=t)
    await state.set_state(AdminCourseSG.price)
    await m.answer("<b>4/5</b>\n\nЦена в рублях (например 4990):")


@router.message(AdminCourseSG.price)
async def cnew_price(m: Message, state: FSMContext) -> None:
    kop = parse_rub_to_kopecks(m.text or "")
    if kop is None or kop <= 0:
        await m.answer("Цена должна быть > 0")
        return
    await state.update_data(price=kop)
    await state.set_state(AdminCourseSG.cover)
    b = InlineKeyboardBuilder()
    b.button(text="Пропустить обложку", callback_data="admin:course_nocover")
    await m.answer(
        "<b>5/5</b>\n\nПришлите обложку (фото/видео/GIF) или нажмите «Пропустить»:",
        reply_markup=b.as_markup(),
    )


async def _save_new_course(data: dict, cover_type: Optional[str], cover_file_id: Optional[str]) -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "INSERT INTO courses(title, short_description, description, price, cover_type, cover_file_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (data["title"], data["short_description"], data["description"],
             data["price"], cover_type, cover_file_id),
        )
        cid = cur.lastrowid
        await conn.commit()
    return cid


@router.callback_query(F.data == "admin:course_nocover", AdminCourseSG.cover)
async def cnew_no_cover(c: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    cid = await _save_new_course(data, None, None)
    await state.clear()
    await c.message.edit_text(
        f"✅ Курс #{cid} создан (черновик, не опубликован).\n"
        f"Добавьте уроки и опубликуйте.",
        reply_markup=kb_back(f"admin:course:{cid}"),
    )
    await c.answer()


@router.message(AdminCourseSG.cover)
async def cnew_cover(m: Message, state: FSMContext) -> None:
    ct = m.content_type
    file_id: Optional[str] = None
    cover_type: Optional[str] = None
    if ct == ContentType.PHOTO and m.photo:
        cover_type = "photo"
        file_id = m.photo[-1].file_id
    elif ct == ContentType.VIDEO and m.video:
        cover_type = "video"
        file_id = m.video.file_id
    elif ct == ContentType.ANIMATION and m.animation:
        cover_type = "animation"
        file_id = m.animation.file_id
    else:
        await m.answer("Нужно фото/видео/GIF. Или нажмите «Пропустить» в сообщении выше.")
        return
    data = await state.get_data()
    cid = await _save_new_course(data, cover_type, file_id)
    await state.clear()
    await m.answer(
        f"✅ Курс #{cid} создан (черновик). Добавьте уроки.",
        reply_markup=kb_back(f"admin:course:{cid}"),
    )


# --- Карточка курса (админ) --- #

async def _course_admin_card(cid: int) -> tuple[Optional[str], Optional[InlineKeyboardMarkup]]:
    rendered = await _render_course(cid)
    if not rendered:
        return None, None
    text, course, lessons = rendered
    text += f"\n\nСтатус: <b>{'опубликован 🟢' if course['is_published'] else 'черновик ⚪️'}</b>"
    b = InlineKeyboardBuilder()
    if course["is_published"]:
        b.button(text="⚪️ Снять с публикации", callback_data=f"admin:course_unpub:{cid}")
    else:
        b.button(text="🟢 Опубликовать", callback_data=f"admin:course_pub:{cid}")
    b.button(text="✏️ Редактировать", callback_data=f"admin:course_edit:{cid}")
    b.button(text="➕ Добавить урок", callback_data=f"admin:lesson_new:{cid}")
    if lessons:
        b.button(text="📋 Уроки", callback_data=f"admin:lessons:{cid}")
    b.button(text="🗑 Удалить курс", callback_data=f"admin:course_del:{cid}")
    b.button(text="⬅️ К списку", callback_data="admin:courses:0")
    b.adjust(1, 2, 2, 1)
    return text, b.as_markup()


@router.callback_query(F.data.startswith("admin:course:"))
async def cb_admin_course(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    cid = int(c.data.split(":")[2])
    text, kb = await _course_admin_card(cid)
    if not text:
        await c.answer("Нет", show_alert=True)
        return
    with suppress(TelegramAPIError):
        await c.message.edit_text(text, reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("admin:course_pub:"))
async def cb_admin_course_pub(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    cid = int(c.data.split(":")[2])
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        lessons = (await (await conn.execute(
            "SELECT COUNT(*) AS c FROM lessons WHERE course_id = ?", (cid,)
        )).fetchone())["c"]
    if lessons == 0:
        await c.answer("Нельзя опубликовать курс без уроков", show_alert=True)
        return
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE courses SET is_published = 1 WHERE id = ?", (cid,)
        )
        await conn.commit()
    await cb_admin_course(c)


@router.callback_query(F.data.startswith("admin:course_unpub:"))
async def cb_admin_course_unpub(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    cid = int(c.data.split(":")[2])
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE courses SET is_published = 0 WHERE id = ?", (cid,)
        )
        await conn.commit()
    await cb_admin_course(c)


@router.callback_query(F.data.startswith("admin:course_del:"))
async def cb_admin_course_del(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    cid = int(c.data.split(":")[2])
    b = InlineKeyboardBuilder()
    b.button(text="🗑 Да, удалить", callback_data=f"admin:course_del_yes:{cid}")
    b.button(text="Отмена", callback_data=f"admin:course:{cid}")
    b.adjust(1)
    await c.message.edit_text(
        f"Удалить курс #{cid} вместе с уроками? Это необратимо.",
        reply_markup=b.as_markup(),
    )
    await c.answer()


@router.callback_query(F.data.startswith("admin:course_del_yes:"))
async def cb_admin_course_del_yes(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    cid = int(c.data.split(":")[2])
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("DELETE FROM lessons WHERE course_id = ?", (cid,))
        await conn.execute("DELETE FROM courses WHERE id = ?", (cid,))
        await conn.commit()
    await c.message.edit_text(
        f"🗑 Курс #{cid} удалён.",
        reply_markup=kb_back("admin:courses:0"),
    )
    await c.answer("Удалено")


# --- Редактирование курса --- #

@router.callback_query(F.data.startswith("admin:course_edit:"))
async def cb_admin_course_edit(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    cid = int(c.data.split(":")[2])
    b = InlineKeyboardBuilder()
    for field, label in [
        ("title", "Название"),
        ("short_description", "Краткое описание"),
        ("description", "Описание"),
        ("price", "Цена"),
    ]:
        b.button(text=f"✏️ {label}", callback_data=f"admin:course_edit_f:{cid}:{field}")
    b.button(text="⬅️ Назад", callback_data=f"admin:course:{cid}")
    b.adjust(2, 2, 1)
    await c.message.edit_text(
        f"Что редактируем в курсе #{cid}?",
        reply_markup=b.as_markup(),
    )
    await c.answer()


@router.callback_query(F.data.startswith("admin:course_edit_f:"))
async def cb_admin_course_edit_field(c: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(c.from_user.id):
        return
    _, _, _, cid_raw, field = c.data.split(":")
    cid = int(cid_raw)
    await state.set_state(AdminEditCourseSG.value)
    await state.update_data(edit_cid=cid, edit_field=field)
    hint = {
        "title": "Новое название:",
        "short_description": "Новое краткое описание:",
        "description": "Новое описание:",
        "price": "Новая цена в рублях:",
    }[field]
    await c.message.edit_text(hint, reply_markup=kb_back(f"admin:course:{cid}"))
    await c.answer()


@router.message(AdminEditCourseSG.value)
async def admin_course_edit_value(m: Message, state: FSMContext) -> None:
    data = await state.get_data()
    cid = int(data["edit_cid"])
    field: str = data["edit_field"]
    value: Any = (m.text or m.html_text or "").strip()
    if field == "price":
        kop = parse_rub_to_kopecks(value)
        if kop is None or kop <= 0:
            await m.answer("Некорректная цена")
            return
        value = kop
    elif field == "title" and not (1 <= len(value) <= 100):
        await m.answer("1..100 символов")
        return
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            f"UPDATE courses SET {field} = ? WHERE id = ?",
            (value, cid),
        )
        await conn.commit()
    await state.clear()
    await m.answer("✅ Обновлено.", reply_markup=kb_admin_menu())


# --- Уроки --- #

@router.callback_query(F.data.startswith("admin:lessons:"))
async def cb_admin_lessons(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    cid = int(c.data.split(":")[2])
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (await conn.execute(
            "SELECT id, title, is_free, position FROM lessons "
            "WHERE course_id = ? ORDER BY position, id",
            (cid,),
        )).fetchall()
    b = InlineKeyboardBuilder()
    for idx, r in enumerate(rows, start=1):
        tag = "🆓 " if r["is_free"] else ""
        b.button(
            text=f"{idx}. {tag}{r['title'][:40]}",
            callback_data=f"admin:lesson:{r['id']}",
        )
    b.button(text="➕ Добавить урок", callback_data=f"admin:lesson_new:{cid}")
    b.button(text="⬅️ К курсу", callback_data=f"admin:course:{cid}")
    b.adjust(1)
    await c.message.edit_text(
        f"<b>📋 Уроки курса #{cid}</b>\nВсего: <b>{len(rows)}</b>",
        reply_markup=b.as_markup(),
    )
    await c.answer()


@router.callback_query(F.data.startswith("admin:lesson_new:"))
async def cb_admin_lesson_new(c: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(c.from_user.id):
        return
    cid = int(c.data.split(":")[2])
    await state.update_data(lesson_course_id=cid)
    await state.set_state(AdminLessonSG.title)
    await c.message.edit_text(
        "<b>Новый урок — 1/4</b>\n\nВведите название урока:",
        reply_markup=kb_back(f"admin:course:{cid}"),
    )
    await c.answer()


@router.message(AdminLessonSG.title)
async def lnew_title(m: Message, state: FSMContext) -> None:
    t = (m.text or "").strip()
    if not (1 <= len(t) <= 100):
        await m.answer("1..100 символов")
        return
    await state.update_data(title=t)
    await state.set_state(AdminLessonSG.description)
    await m.answer("<b>2/4</b>\n\nКраткое описание урока (можно прочерк «-»):")


@router.message(AdminLessonSG.description)
async def lnew_desc(m: Message, state: FSMContext) -> None:
    d = (m.text or m.html_text or "").strip()
    if d == "-":
        d = ""
    await state.update_data(description=d)
    await state.set_state(AdminLessonSG.is_free)
    b = InlineKeyboardBuilder()
    b.button(text="🆓 Бесплатный", callback_data="admin:lnew_free:1")
    b.button(text="🔒 Платный", callback_data="admin:lnew_free:0")
    b.adjust(2)
    await m.answer("<b>3/4</b>\n\nТип урока:", reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("admin:lnew_free:"), AdminLessonSG.is_free)
async def lnew_free(c: CallbackQuery, state: FSMContext) -> None:
    free = c.data.split(":")[2] == "1"
    await state.update_data(is_free=1 if free else 0)
    await state.set_state(AdminLessonSG.content)
    await c.message.edit_text(
        "<b>4/4</b>\n\nПришлите содержимое урока: текст, видео, фото, документ, "
        "аудио, голосовое, GIF, кружок.",
    )
    await c.answer()


@router.message(AdminLessonSG.content)
async def lnew_content(m: Message, state: FSMContext) -> None:
    ct = m.content_type
    file_id: Optional[str] = None
    text_content: Optional[str] = None
    content_type: Optional[str] = None
    caption = m.caption or m.html_text if m.content_type != ContentType.TEXT else None

    if ct == ContentType.TEXT:
        content_type = "text"
        text_content = m.html_text or m.text
    elif ct == ContentType.PHOTO and m.photo:
        content_type = "photo"
        file_id = m.photo[-1].file_id
    elif ct == ContentType.VIDEO and m.video:
        content_type = "video"
        file_id = m.video.file_id
    elif ct == ContentType.DOCUMENT and m.document:
        content_type = "document"
        file_id = m.document.file_id
    elif ct == ContentType.AUDIO and m.audio:
        content_type = "audio"
        file_id = m.audio.file_id
    elif ct == ContentType.VOICE and m.voice:
        content_type = "voice"
        file_id = m.voice.file_id
    elif ct == ContentType.ANIMATION and m.animation:
        content_type = "animation"
        file_id = m.animation.file_id
    elif ct == ContentType.VIDEO_NOTE and m.video_note:
        content_type = "video_note"
        file_id = m.video_note.file_id

    if not content_type or content_type not in SUPPORTED_CONTENT_TYPES:
        await m.answer("Неподдерживаемый тип. Пришлите текст или медиа.")
        return

    data = await state.get_data()
    cid = int(data["lesson_course_id"])
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        max_pos = (await (await conn.execute(
            "SELECT COALESCE(MAX(position), 0) AS p FROM lessons WHERE course_id = ?",
            (cid,),
        )).fetchone())["p"]
        cur = await conn.execute(
            "INSERT INTO lessons(course_id, position, title, description, content_type, "
            "                    content_file_id, content_text, content_caption, is_free) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (cid, max_pos + 1, data["title"], data["description"], content_type,
             file_id, text_content, caption, int(data.get("is_free", 0))),
        )
        lid = cur.lastrowid
        await conn.commit()

    await state.clear()
    await m.answer(
        f"✅ Урок #{lid} добавлен к курсу #{cid}.",
        reply_markup=kb_admin_menu(),
    )


@router.callback_query(F.data.startswith("admin:lesson:"))
async def cb_admin_lesson(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    lid = int(c.data.split(":")[2])
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        lesson = await (await conn.execute(
            "SELECT * FROM lessons WHERE id = ?", (lid,)
        )).fetchone()
    if not lesson:
        await c.answer("Нет", show_alert=True)
        return
    text = (
        f"<b>Урок #{lesson['id']}: {esc(lesson['title'])}</b>\n\n"
        f"{esc(lesson['description'] or '—')}\n\n"
        f"Тип контента: <code>{esc(lesson['content_type'])}</code>\n"
        f"Бесплатный: <b>{'да' if lesson['is_free'] else 'нет'}</b>\n"
        f"Позиция: <b>{lesson['position']}</b>"
    )
    b = InlineKeyboardBuilder()
    b.button(text="🆓/🔒 Перекл. доступ", callback_data=f"admin:lesson_togfree:{lid}")
    b.button(text="⬆️ Вверх", callback_data=f"admin:lesson_up:{lid}")
    b.button(text="⬇️ Вниз", callback_data=f"admin:lesson_down:{lid}")
    b.button(text="✏️ Переименовать", callback_data=f"admin:lesson_edit:{lid}:title")
    b.button(text="📝 Описание", callback_data=f"admin:lesson_edit:{lid}:description")
    b.button(text="🗑 Удалить", callback_data=f"admin:lesson_del:{lid}")
    b.button(text="⬅️ К урокам", callback_data=f"admin:lessons:{lesson['course_id']}")
    b.adjust(1, 2, 2, 1, 1)
    await c.message.edit_text(text, reply_markup=b.as_markup())
    await c.answer()


@router.callback_query(F.data.startswith("admin:lesson_togfree:"))
async def cb_admin_lesson_togfree(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    lid = int(c.data.split(":")[2])
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE lessons SET is_free = 1 - is_free WHERE id = ?", (lid,)
        )
        await conn.commit()
    await cb_admin_lesson(c)


@router.callback_query(F.data.startswith("admin:lesson_up:"))
@router.callback_query(F.data.startswith("admin:lesson_down:"))
async def cb_admin_lesson_move(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    direction = -1 if ":lesson_up:" in c.data else 1
    lid = int(c.data.split(":")[2])
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        lesson = await (await conn.execute(
            "SELECT * FROM lessons WHERE id = ?", (lid,)
        )).fetchone()
        if not lesson:
            await c.answer("Нет", show_alert=True)
            return
        # ищем соседа
        if direction == -1:
            neighbour = await (await conn.execute(
                "SELECT id, position FROM lessons WHERE course_id = ? AND "
                "(position < ? OR (position = ? AND id < ?)) "
                "ORDER BY position DESC, id DESC LIMIT 1",
                (lesson["course_id"], lesson["position"], lesson["position"], lid),
            )).fetchone()
        else:
            neighbour = await (await conn.execute(
                "SELECT id, position FROM lessons WHERE course_id = ? AND "
                "(position > ? OR (position = ? AND id > ?)) "
                "ORDER BY position ASC, id ASC LIMIT 1",
                (lesson["course_id"], lesson["position"], lesson["position"], lid),
            )).fetchone()
        if not neighbour:
            await c.answer("Крайний урок", show_alert=True)
            return
        # меняем позиции
        await conn.execute(
            "UPDATE lessons SET position = ? WHERE id = ?",
            (neighbour["position"], lid),
        )
        await conn.execute(
            "UPDATE lessons SET position = ? WHERE id = ?",
            (lesson["position"], neighbour["id"]),
        )
        await conn.commit()
    await cb_admin_lesson(c)


@router.callback_query(F.data.startswith("admin:lesson_del:"))
async def cb_admin_lesson_del(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    lid = int(c.data.split(":")[2])
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        lesson = await (await conn.execute(
            "SELECT course_id FROM lessons WHERE id = ?", (lid,)
        )).fetchone()
        if not lesson:
            await c.answer("Нет", show_alert=True)
            return
        await conn.execute("DELETE FROM lessons WHERE id = ?", (lid,))
        await conn.commit()
    await c.message.edit_text(
        f"🗑 Урок #{lid} удалён.",
        reply_markup=kb_back(f"admin:lessons:{lesson['course_id']}"),
    )
    await c.answer()


@router.callback_query(F.data.startswith("admin:lesson_edit:"))
async def cb_admin_lesson_edit(c: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(c.from_user.id):
        return
    _, _, lid_raw, field = c.data.split(":")
    lid = int(lid_raw)
    await state.update_data(edit_lid=lid, edit_field=field)
    await state.set_state(AdminLessonEditSG.value)
    await c.message.edit_text(
        f"Новое значение ({field}):",
        reply_markup=kb_back(f"admin:lesson:{lid}"),
    )
    await c.answer()


@router.message(AdminLessonEditSG.value)
async def admin_lesson_edit_value(m: Message, state: FSMContext) -> None:
    data = await state.get_data()
    lid = int(data["edit_lid"])
    field = data["edit_field"]
    value = (m.text or m.html_text or "").strip()
    if field == "title" and not (1 <= len(value) <= 100):
        await m.answer("1..100 символов")
        return
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            f"UPDATE lessons SET {field} = ? WHERE id = ?", (value, lid)
        )
        await conn.commit()
    await state.clear()
    await m.answer("✅ Обновлено.", reply_markup=kb_admin_menu())


# --- Промокоды --- #

@router.callback_query(F.data == "admin:promos")
async def cb_admin_promos(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (await conn.execute(
            "SELECT id, code, discount_percent, usage_limit, used_count, active "
            "FROM promo_codes ORDER BY id DESC LIMIT 20"
        )).fetchall()
    text = "<b>🏷 Промокоды</b>\n\n"
    if not rows:
        text += "Нет промокодов."
    else:
        for r in rows:
            status = "🟢" if r["active"] else "⚪️"
            limit_str = str(r["usage_limit"]) if r["usage_limit"] is not None else "∞"
            text += (
                f"{status} <code>{esc(r['code'])}</code> "
                f"— <b>{r['discount_percent']}%</b> "
                f"({r['used_count']}/{limit_str})\n"
            )
    b = InlineKeyboardBuilder()
    b.button(text="➕ Создать", callback_data="admin:promo_new")
    for r in rows:
        if r["active"]:
            b.button(
                text=f"🗑 {r['code']}",
                callback_data=f"admin:promo_del:{r['id']}",
            )
    b.button(text="⬅️ Админ", callback_data="admin:menu")
    b.adjust(1)
    await c.message.edit_text(text, reply_markup=b.as_markup())
    await c.answer()


@router.callback_query(F.data == "admin:promo_new")
async def cb_admin_promo_new(c: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(c.from_user.id):
        return
    await state.set_state(AdminSG.promo_code)
    suggested = random_promo_code(6)
    await state.update_data(suggested_code=suggested)
    b = InlineKeyboardBuilder()
    b.button(text=f"Использовать {suggested}", callback_data="admin:promo_use_sug")
    b.button(text="⬅️ Отмена", callback_data="admin:promos")
    b.adjust(1)
    await c.message.edit_text(
        f"Введите код промокода (латиница/цифры, 3-20 симв.) или используйте предложенный: <code>{suggested}</code>",
        reply_markup=b.as_markup(),
    )
    await c.answer()


@router.callback_query(F.data == "admin:promo_use_sug", AdminSG.promo_code)
async def cb_admin_promo_use_sug(c: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    code = data.get("suggested_code", random_promo_code(6))
    await state.update_data(promo_code=code.upper())
    await state.set_state(AdminSG.promo_discount)
    await c.message.edit_text(
        f"Код: <code>{code}</code>\n\nВведите % скидки (1–100):"
    )
    await c.answer()


@router.message(AdminSG.promo_code)
async def admin_promo_code(m: Message, state: FSMContext) -> None:
    code = (m.text or "").strip().upper()
    if not (3 <= len(code) <= 20) or not all(ch.isalnum() or ch in "-_" for ch in code):
        await m.answer("3–20 символов, латиница/цифры/-/_")
        return
    async with aiosqlite.connect(DB_PATH) as conn:
        existing = await (await conn.execute(
            "SELECT 1 FROM promo_codes WHERE code = ?", (code,)
        )).fetchone()
    if existing:
        await m.answer("Такой код уже есть")
        return
    await state.update_data(promo_code=code)
    await state.set_state(AdminSG.promo_discount)
    await m.answer("Введите % скидки (1–100):")


@router.message(AdminSG.promo_discount)
async def admin_promo_discount(m: Message, state: FSMContext) -> None:
    try:
        v = int((m.text or "").strip())
    except ValueError:
        await m.answer("Нужно число 1..100")
        return
    if not 1 <= v <= 100:
        await m.answer("Диапазон 1..100")
        return
    await state.update_data(promo_percent=v)
    await state.set_state(AdminSG.promo_limit)
    b = InlineKeyboardBuilder()
    b.button(text="Без лимита", callback_data="admin:promo_nolim")
    await m.answer("Введите максимум использований или нажмите «Без лимита»:",
                   reply_markup=b.as_markup())


async def _save_promo(code: str, pct: int, limit: Optional[int]) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT INTO promo_codes(code, discount_percent, usage_limit) "
            "VALUES (?, ?, ?)",
            (code.upper(), pct, limit),
        )
        await conn.commit()


@router.callback_query(F.data == "admin:promo_nolim", AdminSG.promo_limit)
async def admin_promo_nolim(c: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await _save_promo(data["promo_code"], int(data["promo_percent"]), None)
    await state.clear()
    await c.message.edit_text(
        f"✅ Промокод <code>{data['promo_code']}</code> создан (без лимита).",
        reply_markup=kb_back("admin:promos"),
    )
    await c.answer()


@router.message(AdminSG.promo_limit)
async def admin_promo_limit(m: Message, state: FSMContext) -> None:
    try:
        v = int((m.text or "").strip())
    except ValueError:
        await m.answer("Нужно число")
        return
    if v <= 0:
        await m.answer("> 0")
        return
    data = await state.get_data()
    await _save_promo(data["promo_code"], int(data["promo_percent"]), v)
    await state.clear()
    await m.answer(
        f"✅ Промокод <code>{data['promo_code']}</code> создан (лимит {v}).",
        reply_markup=kb_admin_menu(),
    )


@router.callback_query(F.data.startswith("admin:promo_del:"))
async def cb_admin_promo_del(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    pid = int(c.data.split(":")[2])
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE promo_codes SET active = 0 WHERE id = ?", (pid,)
        )
        await conn.commit()
    await cb_admin_promos(c)


# --------------------------------------------------------------------------- #
# Fallback
# --------------------------------------------------------------------------- #

@router.message(StateFilter(None))
async def fallback_any(m: Message) -> None:
    if m.text and m.text.startswith("/"):
        return
    await m.answer(
        "Используйте меню или /start, /admin.",
        reply_markup=kb_main(is_admin(m.from_user.id)),
    )


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #

async def main() -> None:
    await init_db()
    await set_bot_commands()
    log.info("Course bot starting…")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Course bot stopped.")
