"""
Telegram-бот: публичный магазин цифровых товаров.

Стек: Python 3.10+, aiogram 3.26.0, SQLite (aiosqlite), HTML-разметка.
Платежи: CryptoBot (Crypto Pay API) + ручной перевод по реквизитам админа.

Конфигурация через .env (см. .env.example).
Запуск: python bot.py
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import sys
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ContentType, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    CallbackQuery,
    InlineKeyboardButton,
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
    raise SystemExit("ADMIN_IDS не заданы в .env (через запятую)")

LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0") or 0)
if not LOG_CHANNEL_ID:
    raise SystemExit("LOG_CHANNEL_ID не задан в .env")

CRYPTO_PAY_TOKEN = os.getenv("CRYPTO_PAY_TOKEN", "").strip()
CRYPTO_PAY_TESTNET = os.getenv("CRYPTO_PAY_TESTNET", "false").lower() == "true"
CRYPTO_PAY_BASE = (
    "https://testnet-pay.crypt.bot/api" if CRYPTO_PAY_TESTNET else "https://pay.crypt.bot/api"
)

DB_PATH = os.getenv("DB_PATH", "shop.db")

DEFAULT_COMMISSION_PERCENT = 10  # комиссия на вывод по умолчанию, % (меняется в админке)

# Типы контента товара, которые мы поддерживаем
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("shop-bot")

# --------------------------------------------------------------------------- #
# БД
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id       INTEGER PRIMARY KEY,
    username    TEXT,
    full_name   TEXT,
    balance     INTEGER NOT NULL DEFAULT 0,              -- в копейках
    is_banned   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS products (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_id       INTEGER NOT NULL,
    title           TEXT    NOT NULL,
    description     TEXT    NOT NULL,
    price           INTEGER NOT NULL,                    -- в копейках
    content_type    TEXT    NOT NULL,
    content_file_id TEXT,
    content_text    TEXT,
    content_caption TEXT,
    status          TEXT    NOT NULL DEFAULT 'pending',  -- pending/approved/rejected/archived
    moderation_chat_id   INTEGER,
    moderation_msg_id    INTEGER,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS purchases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  INTEGER NOT NULL,
    buyer_id    INTEGER NOT NULL,
    seller_id   INTEGER NOT NULL,
    price       INTEGER NOT NULL,                        -- в копейках
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS transactions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    kind        TEXT    NOT NULL,  -- deposit_crypto / deposit_manual / purchase / sale / withdraw / admin_credit / commission
    amount      INTEGER NOT NULL,  -- в копейках (знак соответствует направлению)
    status      TEXT    NOT NULL DEFAULT 'done',        -- pending/done/rejected
    details     TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS deposits (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL,
    method         TEXT    NOT NULL,                    -- crypto/manual
    amount         INTEGER NOT NULL,                    -- в копейках
    status         TEXT    NOT NULL DEFAULT 'pending',  -- pending/paid/rejected
    invoice_id     TEXT,                                -- для CryptoBot
    invoice_url    TEXT,
    moderation_msg_id INTEGER,                          -- для ручного пополнения
    created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS withdraws (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    amount      INTEGER NOT NULL,                       -- запрошенная сумма в копейках
    fee         INTEGER NOT NULL,                       -- комиссия в копейках
    payout      INTEGER NOT NULL,                       -- сумма к выплате
    card        TEXT    NOT NULL,
    fio         TEXT    NOT NULL,
    bank        TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'pending',    -- pending/paid/rejected
    moderation_msg_id INTEGER,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""


async def db() -> aiosqlite.Connection:
    """Открыть соединение с включённым row_factory и FK."""
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON;")
    return conn


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.executescript(SCHEMA)
        # дефолтные настройки
        await conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
            ("commission_percent", str(DEFAULT_COMMISSION_PERCENT)),
        )
        await conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
            ("requisites", ""),
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


async def ensure_user(tg_id: int, username: Optional[str], full_name: str) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT INTO users(tg_id, username, full_name) VALUES (?, ?, ?) "
            "ON CONFLICT(tg_id) DO UPDATE SET username = excluded.username, "
            "                                 full_name = excluded.full_name",
            (tg_id, username or "", full_name),
        )
        await conn.commit()


async def get_user(tg_id: int) -> Optional[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        return await (await conn.execute(
            "SELECT * FROM users WHERE tg_id = ?", (tg_id,)
        )).fetchone()


async def change_balance(tg_id: int, delta_kop: int, kind: str, details: str = "") -> int:
    """Изменить баланс и записать транзакцию. Возвращает новый баланс."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(
            "INSERT OR IGNORE INTO users(tg_id, username, full_name) VALUES (?, '', '')",
            (tg_id,),
        )
        await conn.execute(
            "UPDATE users SET balance = balance + ? WHERE tg_id = ?",
            (delta_kop, tg_id),
        )
        await conn.execute(
            "INSERT INTO transactions(user_id, kind, amount, status, details) "
            "VALUES (?, ?, ?, 'done', ?)",
            (tg_id, kind, delta_kop, details),
        )
        row = await (await conn.execute(
            "SELECT balance FROM users WHERE tg_id = ?", (tg_id,)
        )).fetchone()
        await conn.commit()
        return row["balance"] if row else 0


# --------------------------------------------------------------------------- #
# Вспомогательные функции
# --------------------------------------------------------------------------- #

def rub(kopecks: int) -> str:
    """Отформатировать копейки как рубли, HTML-safe."""
    sign = "-" if kopecks < 0 else ""
    kopecks = abs(int(kopecks))
    rubles, kop = divmod(kopecks, 100)
    return f"{sign}{rubles:,}".replace(",", " ") + f",{kop:02d} ₽"


def parse_rub_to_kopecks(raw: str) -> Optional[int]:
    """'199', '199.5', '199,50' -> копейки. None если невалидно."""
    s = raw.strip().replace(" ", "").replace(",", ".")
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


async def get_commission_percent() -> int:
    raw = await setting_get("commission_percent", str(DEFAULT_COMMISSION_PERCENT))
    try:
        return max(0, min(100, int(raw)))
    except ValueError:
        return DEFAULT_COMMISSION_PERCENT


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

    async def create_invoice(self, amount_rub: float, description: str, payload: str) -> dict[str, Any]:
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

class SellSG(StatesGroup):
    title = State()
    description = State()
    price = State()
    content = State()


class TopUpSG(StatesGroup):
    method = State()
    amount_crypto = State()
    amount_manual = State()
    wait_manual_proof = State()


class WithdrawSG(StatesGroup):
    amount = State()
    card = State()
    fio = State()
    bank = State()


class AdminSG(StatesGroup):
    broadcast_text = State()
    give_balance_user = State()
    give_balance_amount = State()
    set_commission = State()
    set_requisites = State()
    edit_product_price = State()


# --------------------------------------------------------------------------- #
# Клавиатуры
# --------------------------------------------------------------------------- #

def kb_main(is_admin_user: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🛒 Каталог", callback_data="catalog:0")
    b.button(text="➕ Продать товар", callback_data="sell:start")
    b.button(text="👤 Мой профиль", callback_data="profile")
    b.button(text="💰 Пополнить", callback_data="topup:menu")
    b.button(text="💸 Вывести", callback_data="withdraw:start")
    b.button(text="📦 Мои товары", callback_data="my_products:0")
    if is_admin_user:
        b.button(text="🛠 Админ-панель", callback_data="admin:menu")
    b.adjust(2, 2, 2, 1)
    return b.as_markup()


def kb_back(cb: str = "main") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Назад", callback_data=cb)
    return b.as_markup()


def kb_topup_methods() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if crypto_pay.enabled:
        b.button(text="🪙 CryptoBot", callback_data="topup:crypto")
    b.button(text="💳 Перевод на карту", callback_data="topup:manual")
    b.button(text="⬅️ Назад", callback_data="main")
    b.adjust(1)
    return b.as_markup()


def kb_admin_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📊 Статистика", callback_data="admin:stats")
    b.button(text="📣 Рассылка", callback_data="admin:broadcast")
    b.button(text="💵 Выдать баланс", callback_data="admin:give")
    b.button(text="💳 Реквизиты", callback_data="admin:req")
    b.button(text="📦 Товары", callback_data="admin:products:0")
    b.button(text="⚙️ Комиссия вывода", callback_data="admin:commission")
    b.button(text="⬅️ В меню", callback_data="main")
    b.adjust(2, 2, 2, 1)
    return b.as_markup()


def kb_moderation_product(pid: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Одобрить", callback_data=f"mod:product:approve:{pid}")
    b.button(text="❌ Отклонить", callback_data=f"mod:product:reject:{pid}")
    b.adjust(2)
    return b.as_markup()


def kb_moderation_product_approved() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Одобрено", callback_data="noop")
    return b.as_markup()


def kb_moderation_product_rejected() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отклонено", callback_data="noop")
    return b.as_markup()


def kb_moderation_deposit(did: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Подтвердить оплату", callback_data=f"mod:deposit:approve:{did}")
    b.button(text="❌ Отклонить", callback_data=f"mod:deposit:reject:{did}")
    b.adjust(1)
    return b.as_markup()


def kb_moderation_withdraw(wid: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Выплачено", callback_data=f"mod:withdraw:approve:{wid}")
    b.button(text="❌ Отклонить", callback_data=f"mod:withdraw:reject:{wid}")
    b.adjust(1)
    return b.as_markup()


# --------------------------------------------------------------------------- #
# Бот
# --------------------------------------------------------------------------- #

bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


async def set_bot_commands() -> None:
    """Регистрирует команды, чтобы они показывались при вводе '/'."""
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
# Общие команды
# --------------------------------------------------------------------------- #

@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext) -> None:
    await state.clear()
    await ensure_user(m.from_user.id, m.from_user.username, m.from_user.full_name)
    await m.answer(
        "<b>🛍 Магазин цифровых товаров</b>\n\n"
        "Здесь можно покупать и продавать товары — медиа, тексты, файлы.\n"
        "Деньги зачисляются на внутренний баланс, вывод — на карту.",
        reply_markup=kb_main(is_admin(m.from_user.id)),
    )


@router.message(Command("admin"))
async def cmd_admin(m: Message, state: FSMContext) -> None:
    if not is_admin(m.from_user.id):
        return
    await state.clear()
    await m.answer("<b>🛠 Админ-панель</b>", reply_markup=kb_admin_menu())


@router.callback_query(F.data == "main")
async def cb_main(c: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await c.message.edit_text(
        "<b>🛍 Магазин цифровых товаров</b>",
        reply_markup=kb_main(is_admin(c.from_user.id)),
    )
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
    commission = await get_commission_percent()
    text = (
        f"<b>👤 Профиль</b>\n\n"
        f"ID: <code>{c.from_user.id}</code>\n"
        f"Баланс: <b>{esc(rub(balance))}</b>\n"
        f"Комиссия на вывод: <b>{commission}%</b>"
    )
    await c.message.edit_text(text, reply_markup=kb_back("main"))
    await c.answer()


# --------------------------------------------------------------------------- #
# Каталог / покупка
# --------------------------------------------------------------------------- #

PAGE_SIZE = 5


@router.callback_query(F.data.startswith("catalog:"))
async def cb_catalog(c: CallbackQuery) -> None:
    page = int(c.data.split(":")[1])
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (await conn.execute(
            "SELECT id, title, price FROM products "
            "WHERE status = 'approved' "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            (PAGE_SIZE, page * PAGE_SIZE),
        )).fetchall()
        total = (await (await conn.execute(
            "SELECT COUNT(*) AS c FROM products WHERE status = 'approved'"
        )).fetchone())["c"]
    if not rows and page == 0:
        await c.message.edit_text("🛒 Каталог пока пуст.", reply_markup=kb_back("main"))
        await c.answer()
        return

    b = InlineKeyboardBuilder()
    for r in rows:
        b.button(
            text=f"{r['title'][:40]} — {rub(r['price'])}",
            callback_data=f"prod:view:{r['id']}",
        )
    b.adjust(1)
    # пагинация
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
        f"<b>🛒 Каталог</b>\nВсего товаров: <b>{total}</b>",
        reply_markup=b.as_markup(),
    )
    await c.answer()


async def render_product_card(pid: int) -> Optional[tuple[str, aiosqlite.Row]]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        p = await (await conn.execute(
            "SELECT * FROM products WHERE id = ?", (pid,)
        )).fetchone()
        if not p:
            return None
        seller = await (await conn.execute(
            "SELECT username, full_name FROM users WHERE tg_id = ?", (p["seller_id"],)
        )).fetchone()
    seller_name = ""
    if seller:
        if seller["username"]:
            seller_name = f"@{esc(seller['username'])}"
        else:
            seller_name = esc(seller["full_name"] or p["seller_id"])
    text = (
        f"<b>{esc(p['title'])}</b>\n\n"
        f"{esc(p['description'])}\n\n"
        f"💰 Цена: <b>{esc(rub(p['price']))}</b>\n"
        f"👤 Продавец: {seller_name}\n"
        f"🆔 Товар: <code>#{p['id']}</code>"
    )
    return text, p


@router.callback_query(F.data.startswith("prod:view:"))
async def cb_product_view(c: CallbackQuery) -> None:
    pid = int(c.data.split(":")[2])
    rendered = await render_product_card(pid)
    if not rendered:
        await c.answer("Товар не найден", show_alert=True)
        return
    text, p = rendered
    b = InlineKeyboardBuilder()
    if p["status"] == "approved" and p["seller_id"] != c.from_user.id:
        b.button(text=f"🛒 Купить за {rub(p['price'])}", callback_data=f"prod:buy:{pid}")
    b.button(text="⬅️ В каталог", callback_data="catalog:0")
    b.adjust(1)
    await c.message.edit_text(text, reply_markup=b.as_markup())
    await c.answer()


@router.callback_query(F.data.startswith("prod:buy:"))
async def cb_product_buy(c: CallbackQuery) -> None:
    pid = int(c.data.split(":")[2])
    rendered = await render_product_card(pid)
    if not rendered:
        await c.answer("Товар не найден", show_alert=True)
        return
    _, p = rendered
    if p["status"] != "approved":
        await c.answer("Товар недоступен", show_alert=True)
        return
    if p["seller_id"] == c.from_user.id:
        await c.answer("Нельзя купить собственный товар", show_alert=True)
        return
    u = await get_user(c.from_user.id)
    if not u or u["balance"] < p["price"]:
        await c.answer("Недостаточно средств, пополните баланс", show_alert=True)
        return
    # списание покупателю
    await change_balance(c.from_user.id, -int(p["price"]), "purchase", f"product #{pid}")
    # зачисление продавцу
    await change_balance(int(p["seller_id"]), int(p["price"]), "sale", f"product #{pid}")
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT INTO purchases(product_id, buyer_id, seller_id, price) "
            "VALUES (?, ?, ?, ?)",
            (pid, c.from_user.id, p["seller_id"], p["price"]),
        )
        await conn.commit()

    # доставка контента покупателю
    await deliver_product_content(c.from_user.id, p)
    with suppress(TelegramAPIError):
        await bot.send_message(
            int(p["seller_id"]),
            f"💸 Ваш товар <b>{esc(p['title'])}</b> купили за <b>{esc(rub(p['price']))}</b>.",
        )
    await c.answer("Оплачено и доставлено!", show_alert=True)


async def deliver_product_content(chat_id: int, p: aiosqlite.Row) -> None:
    caption = p["content_caption"] or ""
    ctype = p["content_type"]
    file_id = p["content_file_id"]
    try:
        if ctype == "text":
            await bot.send_message(chat_id, p["content_text"] or "(пусто)")
        elif ctype == "photo":
            await bot.send_photo(chat_id, file_id, caption=caption or None)
        elif ctype == "video":
            await bot.send_video(chat_id, file_id, caption=caption or None)
        elif ctype == "document":
            await bot.send_document(chat_id, file_id, caption=caption or None)
        elif ctype == "audio":
            await bot.send_audio(chat_id, file_id, caption=caption or None)
        elif ctype == "voice":
            await bot.send_voice(chat_id, file_id, caption=caption or None)
        elif ctype == "animation":
            await bot.send_animation(chat_id, file_id, caption=caption or None)
        elif ctype == "video_note":
            await bot.send_video_note(chat_id, file_id)
    except TelegramAPIError as e:
        log.warning("Не удалось доставить товар %s: %s", p["id"], e)


# --------------------------------------------------------------------------- #
# Продажа: FSM
# --------------------------------------------------------------------------- #

@router.callback_query(F.data == "sell:start")
async def cb_sell_start(c: CallbackQuery, state: FSMContext) -> None:
    await ensure_user(c.from_user.id, c.from_user.username, c.from_user.full_name)
    await state.set_state(SellSG.title)
    await c.message.edit_text(
        "<b>➕ Новый товар — шаг 1/4</b>\n\nВведите <b>название</b> товара (до 80 симв.):",
        reply_markup=kb_back("main"),
    )
    await c.answer()


@router.message(SellSG.title)
async def sell_title(m: Message, state: FSMContext) -> None:
    title = (m.text or "").strip()
    if not (1 <= len(title) <= 80):
        await m.answer("Название должно быть от 1 до 80 символов. Повторите.")
        return
    await state.update_data(title=title)
    await state.set_state(SellSG.description)
    await m.answer("<b>Шаг 2/4</b>\n\nВведите <b>описание</b> (до 1000 симв.):")


@router.message(SellSG.description)
async def sell_description(m: Message, state: FSMContext) -> None:
    desc = (m.text or "").strip()
    if not (1 <= len(desc) <= 1000):
        await m.answer("Описание должно быть от 1 до 1000 символов. Повторите.")
        return
    await state.update_data(description=desc)
    await state.set_state(SellSG.price)
    await m.answer("<b>Шаг 3/4</b>\n\nВведите <b>цену в рублях</b> (например: 199 или 199,50):")


@router.message(SellSG.price)
async def sell_price(m: Message, state: FSMContext) -> None:
    price = parse_rub_to_kopecks(m.text or "")
    if price is None or price <= 0:
        await m.answer("Некорректная цена. Повторите.")
        return
    await state.update_data(price=price)
    await state.set_state(SellSG.content)
    await m.answer(
        "<b>Шаг 4/4</b>\n\n"
        "Отправьте <b>содержимое товара</b>: текст, фото, видео, документ, "
        "аудио, голосовое, кружочек или GIF.\n"
        "Именно это получит покупатель после оплаты."
    )


@router.message(SellSG.content)
async def sell_content(m: Message, state: FSMContext) -> None:
    content_type: Optional[str] = None
    file_id: Optional[str] = None
    text_content: Optional[str] = None
    caption: Optional[str] = m.caption or None

    if m.content_type == ContentType.TEXT:
        content_type = "text"
        text_content = m.text
    elif m.content_type == ContentType.PHOTO and m.photo:
        content_type = "photo"
        file_id = m.photo[-1].file_id
    elif m.content_type == ContentType.VIDEO and m.video:
        content_type = "video"
        file_id = m.video.file_id
    elif m.content_type == ContentType.DOCUMENT and m.document:
        content_type = "document"
        file_id = m.document.file_id
    elif m.content_type == ContentType.AUDIO and m.audio:
        content_type = "audio"
        file_id = m.audio.file_id
    elif m.content_type == ContentType.VOICE and m.voice:
        content_type = "voice"
        file_id = m.voice.file_id
    elif m.content_type == ContentType.ANIMATION and m.animation:
        content_type = "animation"
        file_id = m.animation.file_id
    elif m.content_type == ContentType.VIDEO_NOTE and m.video_note:
        content_type = "video_note"
        file_id = m.video_note.file_id

    if not content_type or content_type not in SUPPORTED_CONTENT_TYPES:
        await m.answer("Неподдерживаемый тип контента. Отправьте текст, медиа или файл.")
        return

    data = await state.get_data()
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "INSERT INTO products(seller_id, title, description, price, "
            "                     content_type, content_file_id, content_text, content_caption) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                m.from_user.id,
                data["title"],
                data["description"],
                data["price"],
                content_type,
                file_id,
                text_content,
                caption,
            ),
        )
        pid = cur.lastrowid
        await conn.commit()

    await state.clear()
    await m.answer(
        f"✅ Товар <b>#{pid}</b> отправлен на модерацию. Мы уведомим вас о решении.",
        reply_markup=kb_main(is_admin(m.from_user.id)),
    )
    await send_product_to_moderation(pid)


async def send_product_to_moderation(pid: int) -> None:
    rendered = await render_product_card(pid)
    if not rendered:
        return
    text, p = rendered
    header = f"🆕 <b>Модерация товара</b>\n\n"
    full = header + text

    try:
        if p["content_type"] == "text":
            msg = await bot.send_message(
                LOG_CHANNEL_ID,
                full + "\n\n<b>Материал:</b>\n" + esc(p["content_text"] or ""),
                reply_markup=kb_moderation_product(pid),
            )
        else:
            # отправляем медиа с подписью-карточкой
            kw = {
                "chat_id": LOG_CHANNEL_ID,
                "caption": full,
                "reply_markup": kb_moderation_product(pid),
            }
            ct = p["content_type"]
            fid = p["content_file_id"]
            if ct == "photo":
                msg = await bot.send_photo(photo=fid, **kw)
            elif ct == "video":
                msg = await bot.send_video(video=fid, **kw)
            elif ct == "document":
                msg = await bot.send_document(document=fid, **kw)
            elif ct == "audio":
                msg = await bot.send_audio(audio=fid, **kw)
            elif ct == "voice":
                # voice не поддерживает caption в некоторых клиентах — отправим отдельно
                msg_txt = await bot.send_message(
                    LOG_CHANNEL_ID, full, reply_markup=kb_moderation_product(pid)
                )
                await bot.send_voice(LOG_CHANNEL_ID, fid)
                msg = msg_txt
            elif ct == "animation":
                msg = await bot.send_animation(animation=fid, **kw)
            elif ct == "video_note":
                msg_txt = await bot.send_message(
                    LOG_CHANNEL_ID, full, reply_markup=kb_moderation_product(pid)
                )
                await bot.send_video_note(LOG_CHANNEL_ID, fid)
                msg = msg_txt
            else:
                msg = await bot.send_message(
                    LOG_CHANNEL_ID, full, reply_markup=kb_moderation_product(pid)
                )
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                "UPDATE products SET moderation_chat_id = ?, moderation_msg_id = ? WHERE id = ?",
                (msg.chat.id, msg.message_id, pid),
            )
            await conn.commit()
    except TelegramAPIError as e:
        log.exception("send_product_to_moderation failed: %s", e)


# --------------------------------------------------------------------------- #
# Модерация товара — колбэки
# --------------------------------------------------------------------------- #

@router.callback_query(F.data.startswith("mod:product:"))
async def cb_moderate_product(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        await c.answer("Нет прав", show_alert=True)
        return
    _, _, action, pid_raw = c.data.split(":")
    pid = int(pid_raw)
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        p = await (await conn.execute(
            "SELECT * FROM products WHERE id = ?", (pid,)
        )).fetchone()
    if not p:
        await c.answer("Товар не найден", show_alert=True)
        return
    if p["status"] != "pending":
        await c.answer("Уже обработано", show_alert=True)
        return

    new_status = "approved" if action == "approve" else "rejected"
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE products SET status = ? WHERE id = ?", (new_status, pid)
        )
        await conn.commit()

    # обновляем кнопки карточки
    new_kb = (
        kb_moderation_product_approved()
        if new_status == "approved"
        else kb_moderation_product_rejected()
    )
    with suppress(TelegramAPIError):
        await c.message.edit_reply_markup(reply_markup=new_kb)

    # уведомляем продавца
    with suppress(TelegramAPIError):
        if new_status == "approved":
            await bot.send_message(
                int(p["seller_id"]),
                f"✅ Ваш товар <b>{esc(p['title'])}</b> одобрен и опубликован в каталоге.",
            )
        else:
            await bot.send_message(
                int(p["seller_id"]),
                f"❌ Товар <b>{esc(p['title'])}</b> отклонён модерацией.",
            )

    await c.answer("Готово")


# --------------------------------------------------------------------------- #
# Мои товары
# --------------------------------------------------------------------------- #

@router.callback_query(F.data.startswith("my_products:"))
async def cb_my_products(c: CallbackQuery) -> None:
    page = int(c.data.split(":")[1])
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (await conn.execute(
            "SELECT id, title, price, status FROM products "
            "WHERE seller_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
            (c.from_user.id, PAGE_SIZE, page * PAGE_SIZE),
        )).fetchall()
    if not rows and page == 0:
        await c.message.edit_text(
            "📦 У вас пока нет товаров.", reply_markup=kb_back("main")
        )
        await c.answer()
        return
    b = InlineKeyboardBuilder()
    for r in rows:
        status_emoji = {
            "pending": "⏳",
            "approved": "✅",
            "rejected": "❌",
            "archived": "📭",
        }.get(r["status"], "•")
        b.button(
            text=f"{status_emoji} {r['title'][:30]} — {rub(r['price'])}",
            callback_data=f"prod:view:{r['id']}",
        )
    b.adjust(1)
    back = InlineKeyboardBuilder()
    back.button(text="⬅️ В меню", callback_data="main")
    b.attach(back)
    await c.message.edit_text(
        "<b>📦 Ваши товары</b>", reply_markup=b.as_markup()
    )
    await c.answer()


# --------------------------------------------------------------------------- #
# Пополнение баланса
# --------------------------------------------------------------------------- #

@router.callback_query(F.data == "topup:menu")
async def cb_topup_menu(c: CallbackQuery) -> None:
    await c.message.edit_text(
        "<b>💰 Пополнение баланса</b>\n\nВыберите способ:",
        reply_markup=kb_topup_methods(),
    )
    await c.answer()


# --- CryptoBot --- #

@router.callback_query(F.data == "topup:crypto")
async def cb_topup_crypto(c: CallbackQuery, state: FSMContext) -> None:
    if not crypto_pay.enabled:
        await c.answer("CryptoBot не настроен", show_alert=True)
        return
    await state.set_state(TopUpSG.amount_crypto)
    await c.message.edit_text(
        "🪙 Введите сумму в рублях для пополнения через <b>CryptoBot</b>:",
        reply_markup=kb_back("topup:menu"),
    )
    await c.answer()


@router.message(TopUpSG.amount_crypto)
async def topup_crypto_amount(m: Message, state: FSMContext) -> None:
    kop = parse_rub_to_kopecks(m.text or "")
    if kop is None or kop < 100:
        await m.answer("Минимальная сумма — 1 ₽. Введите корректную сумму.")
        return
    try:
        inv = await crypto_pay.create_invoice(
            amount_rub=kop / 100,
            description=f"Пополнение баланса @ {m.from_user.id}",
            payload=f"user={m.from_user.id}",
        )
    except Exception as e:
        log.exception("createInvoice error: %s", e)
        await m.answer("Не удалось создать счёт CryptoBot. Попробуйте позже.")
        await state.clear()
        return

    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "INSERT INTO deposits(user_id, method, amount, status, invoice_id, invoice_url) "
            "VALUES (?, 'crypto', ?, 'pending', ?, ?)",
            (m.from_user.id, kop, str(inv["invoice_id"]), inv.get("bot_invoice_url") or inv.get("pay_url")),
        )
        did = cur.lastrowid
        await conn.commit()

    b = InlineKeyboardBuilder()
    b.button(text="💳 Оплатить", url=inv.get("bot_invoice_url") or inv.get("pay_url"))
    b.button(text="🔄 Проверить оплату", callback_data=f"topup:crypto:check:{did}")
    b.button(text="⬅️ Назад", callback_data="topup:menu")
    b.adjust(1)
    await m.answer(
        f"Счёт на <b>{esc(rub(kop))}</b> создан.\n"
        "Оплатите по кнопке ниже и нажмите «Проверить оплату».",
        reply_markup=b.as_markup(),
    )
    await state.clear()


@router.callback_query(F.data.startswith("topup:crypto:check:"))
async def cb_topup_crypto_check(c: CallbackQuery) -> None:
    did = int(c.data.split(":")[3])
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        dep = await (await conn.execute(
            "SELECT * FROM deposits WHERE id = ?", (did,)
        )).fetchone()
    if not dep or dep["user_id"] != c.from_user.id:
        await c.answer("Не найдено", show_alert=True)
        return
    if dep["status"] == "paid":
        await c.answer("Уже зачислено", show_alert=True)
        return
    try:
        items = await crypto_pay.get_invoices([dep["invoice_id"]])
    except Exception as e:
        log.exception("getInvoices error: %s", e)
        await c.answer("Ошибка запроса, попробуйте позже", show_alert=True)
        return
    if not items:
        await c.answer("Счёт не найден в CryptoBot", show_alert=True)
        return
    inv = items[0]
    if inv.get("status") == "paid":
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                "UPDATE deposits SET status = 'paid' WHERE id = ?", (did,)
            )
            await conn.commit()
        await change_balance(
            c.from_user.id, int(dep["amount"]),
            "deposit_crypto", f"invoice {dep['invoice_id']}",
        )
        await c.message.edit_text(
            f"✅ Оплата получена. Баланс пополнен на <b>{esc(rub(int(dep['amount'])))}</b>.",
            reply_markup=kb_back("main"),
        )
        await c.answer("Оплачено!")
    else:
        await c.answer("Оплата ещё не поступила", show_alert=True)


# --- Ручной перевод --- #

@router.callback_query(F.data == "topup:manual")
async def cb_topup_manual(c: CallbackQuery, state: FSMContext) -> None:
    req = await setting_get("requisites", "")
    if not req.strip():
        await c.answer("Реквизиты ещё не настроены администратором", show_alert=True)
        return
    await state.set_state(TopUpSG.amount_manual)
    await c.message.edit_text(
        "<b>💳 Ручное пополнение</b>\n\n"
        "Введите сумму в рублях, которую хотите пополнить:",
        reply_markup=kb_back("topup:menu"),
    )
    await c.answer()


@router.message(TopUpSG.amount_manual)
async def topup_manual_amount(m: Message, state: FSMContext) -> None:
    kop = parse_rub_to_kopecks(m.text or "")
    if kop is None or kop < 100:
        await m.answer("Минимальная сумма — 1 ₽. Повторите.")
        return
    await state.update_data(amount_manual=kop)
    req = await setting_get("requisites", "(не задано)")
    await state.set_state(TopUpSG.wait_manual_proof)
    await m.answer(
        f"Реквизиты для перевода:\n<blockquote>{esc(req)}</blockquote>\n"
        f"Переведите <b>{esc(rub(kop))}</b> и пришлите скрин/документ-подтверждение.",
        reply_markup=kb_back("topup:menu"),
    )


@router.message(TopUpSG.wait_manual_proof)
async def topup_manual_proof(m: Message, state: FSMContext) -> None:
    data = await state.get_data()
    kop: int = int(data.get("amount_manual", 0))
    if kop <= 0:
        await state.clear()
        return
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "INSERT INTO deposits(user_id, method, amount, status) "
            "VALUES (?, 'manual', ?, 'pending')",
            (m.from_user.id, kop),
        )
        did = cur.lastrowid
        await conn.commit()

    # Пересылаем пруф и шлём карточку в канал модерации
    caption = (
        f"💳 <b>Ручное пополнение #{did}</b>\n"
        f"Юзер: <a href=\"tg://user?id={m.from_user.id}\">{esc(m.from_user.full_name)}</a>"
        f" (<code>{m.from_user.id}</code>)\n"
        f"Сумма: <b>{esc(rub(kop))}</b>"
    )
    try:
        fwd = await m.forward(LOG_CHANNEL_ID)
        msg = await bot.send_message(
            LOG_CHANNEL_ID,
            caption,
            reply_markup=kb_moderation_deposit(did),
            reply_parameters=ReplyParameters(message_id=fwd.message_id),
        )
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                "UPDATE deposits SET moderation_msg_id = ? WHERE id = ?",
                (msg.message_id, did),
            )
            await conn.commit()
    except TelegramAPIError as e:
        log.exception("manual deposit notify failed: %s", e)

    await m.answer(
        "📨 Заявка отправлена администратору. Ожидайте подтверждения.",
        reply_markup=kb_main(is_admin(m.from_user.id)),
    )
    await state.clear()


@router.callback_query(F.data.startswith("mod:deposit:"))
async def cb_mod_deposit(c: CallbackQuery) -> None:
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
    if not dep:
        await c.answer("Заявка не найдена", show_alert=True)
        return
    if dep["status"] != "pending":
        await c.answer("Уже обработана", show_alert=True)
        return

    if action == "approve":
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                "UPDATE deposits SET status = 'paid' WHERE id = ?", (did,)
            )
            await conn.commit()
        await change_balance(
            int(dep["user_id"]), int(dep["amount"]),
            "deposit_manual", f"deposit #{did}",
        )
        with suppress(TelegramAPIError):
            await bot.send_message(
                int(dep["user_id"]),
                f"✅ Пополнение на <b>{esc(rub(int(dep['amount'])))}</b> зачислено.",
            )
        text_suffix = "\n\n✅ Подтверждено"
    else:
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                "UPDATE deposits SET status = 'rejected' WHERE id = ?", (did,)
            )
            await conn.commit()
        with suppress(TelegramAPIError):
            await bot.send_message(
                int(dep["user_id"]),
                f"❌ Пополнение #{did} отклонено.",
            )
        text_suffix = "\n\n❌ Отклонено"

    with suppress(TelegramAPIError):
        await c.message.edit_text(
            (c.message.html_text or c.message.text or "") + text_suffix,
            reply_markup=None,
        )
    await c.answer("Готово")


# --------------------------------------------------------------------------- #
# Вывод средств
# --------------------------------------------------------------------------- #

@router.callback_query(F.data == "withdraw:start")
async def cb_withdraw_start(c: CallbackQuery, state: FSMContext) -> None:
    u = await get_user(c.from_user.id)
    if not u or u["balance"] <= 0:
        await c.answer("На балансе нет средств", show_alert=True)
        return
    commission = await get_commission_percent()
    await state.set_state(WithdrawSG.amount)
    await c.message.edit_text(
        f"<b>💸 Вывод на карту</b>\n\n"
        f"Баланс: <b>{esc(rub(u['balance']))}</b>\n"
        f"Комиссия: <b>{commission}%</b>\n\n"
        f"Введите сумму в рублях (будет списана полностью, на карту придёт сумма за вычетом комиссии):",
        reply_markup=kb_back("main"),
    )
    await c.answer()


@router.message(WithdrawSG.amount)
async def withdraw_amount(m: Message, state: FSMContext) -> None:
    kop = parse_rub_to_kopecks(m.text or "")
    if kop is None or kop < 100:
        await m.answer("Минимальная сумма — 1 ₽. Повторите.")
        return
    u = await get_user(m.from_user.id)
    if not u or u["balance"] < kop:
        await m.answer("Недостаточно средств на балансе.")
        return
    commission = await get_commission_percent()
    fee = kop * commission // 100
    payout = kop - fee
    await state.update_data(amount=kop, fee=fee, payout=payout)
    await state.set_state(WithdrawSG.card)
    await m.answer(
        f"К списанию: <b>{esc(rub(kop))}</b>\n"
        f"Комиссия {commission}%: <b>{esc(rub(fee))}</b>\n"
        f"На карту придёт: <b>{esc(rub(payout))}</b>\n\n"
        "Введите <b>номер карты</b>:"
    )


@router.message(WithdrawSG.card)
async def withdraw_card(m: Message, state: FSMContext) -> None:
    card = (m.text or "").strip()
    digits = "".join(ch for ch in card if ch.isdigit())
    if not (13 <= len(digits) <= 19):
        await m.answer("Похоже, это не номер карты. Повторите.")
        return
    await state.update_data(card=card)
    await state.set_state(WithdrawSG.fio)
    await m.answer("Введите <b>ФИО получателя</b>:")


@router.message(WithdrawSG.fio)
async def withdraw_fio(m: Message, state: FSMContext) -> None:
    fio = (m.text or "").strip()
    if len(fio) < 3:
        await m.answer("Введите ФИО полностью.")
        return
    await state.update_data(fio=fio)
    await state.set_state(WithdrawSG.bank)
    await m.answer("Введите <b>название банка</b>:")


@router.message(WithdrawSG.bank)
async def withdraw_bank(m: Message, state: FSMContext) -> None:
    bank = (m.text or "").strip()
    if not bank:
        await m.answer("Введите банк.")
        return
    data = await state.get_data()
    amount = int(data["amount"])
    fee = int(data["fee"])
    payout = int(data["payout"])
    card = data["card"]
    fio = data["fio"]

    # Списываем с баланса сразу (блокируем)
    u = await get_user(m.from_user.id)
    if not u or u["balance"] < amount:
        await m.answer("Баланс изменился, повторите попытку.")
        await state.clear()
        return

    await change_balance(m.from_user.id, -amount, "withdraw", "withdraw request")
    # Комиссия логируется отдельной транзакцией
    if fee > 0:
        await change_balance(m.from_user.id, 0, "commission", f"fee {fee}")

    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "INSERT INTO withdraws(user_id, amount, fee, payout, card, fio, bank) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (m.from_user.id, amount, fee, payout, card, fio, bank),
        )
        wid = cur.lastrowid
        await conn.commit()

    # Уведомляем админ-канал
    text = (
        f"💸 <b>Заявка на вывод #{wid}</b>\n"
        f"Юзер: <a href=\"tg://user?id={m.from_user.id}\">{esc(m.from_user.full_name)}</a>"
        f" (<code>{m.from_user.id}</code>)\n"
        f"Списано: <b>{esc(rub(amount))}</b>\n"
        f"Комиссия: <b>{esc(rub(fee))}</b>\n"
        f"К выплате: <b>{esc(rub(payout))}</b>\n\n"
        f"Карта: <code>{esc(card)}</code>\n"
        f"ФИО: <b>{esc(fio)}</b>\n"
        f"Банк: <b>{esc(bank)}</b>"
    )
    with suppress(TelegramAPIError):
        msg = await bot.send_message(
            LOG_CHANNEL_ID, text, reply_markup=kb_moderation_withdraw(wid)
        )
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                "UPDATE withdraws SET moderation_msg_id = ? WHERE id = ?",
                (msg.message_id, wid),
            )
            await conn.commit()

    await state.clear()
    await m.answer(
        f"✅ Заявка #{wid} создана, средства заморожены. Ожидайте выплаты.",
        reply_markup=kb_main(is_admin(m.from_user.id)),
    )


@router.callback_query(F.data.startswith("mod:withdraw:"))
async def cb_mod_withdraw(c: CallbackQuery) -> None:
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
    if not w:
        await c.answer("Заявка не найдена", show_alert=True)
        return
    if w["status"] != "pending":
        await c.answer("Уже обработана", show_alert=True)
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
                f"✅ Выплата #{wid} на <b>{esc(rub(int(w['payout'])))}</b> отправлена на карту.",
            )
        suffix = "\n\n✅ Выплачено"
    else:
        # возвращаем средства
        await change_balance(
            int(w["user_id"]), int(w["amount"]),
            "admin_credit", f"withdraw #{wid} rejected refund",
        )
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                "UPDATE withdraws SET status = 'rejected' WHERE id = ?", (wid,)
            )
            await conn.commit()
        with suppress(TelegramAPIError):
            await bot.send_message(
                int(w["user_id"]),
                f"❌ Заявка на вывод #{wid} отклонена, средства возвращены на баланс.",
            )
        suffix = "\n\n❌ Отклонено, средства возвращены"

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
        await c.answer("Нет прав", show_alert=True)
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
        users = (await (await conn.execute("SELECT COUNT(*) c FROM users")).fetchone())["c"]
        products_total = (await (await conn.execute(
            "SELECT COUNT(*) c FROM products"
        )).fetchone())["c"]
        products_active = (await (await conn.execute(
            "SELECT COUNT(*) c FROM products WHERE status='approved'"
        )).fetchone())["c"]
        sales = await (await conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(price),0) s FROM purchases"
        )).fetchone()
        deposits_paid = await (await conn.execute(
            "SELECT COALESCE(SUM(amount),0) s FROM deposits WHERE status='paid'"
        )).fetchone()
        withdraws_paid = await (await conn.execute(
            "SELECT COALESCE(SUM(payout),0) s, COALESCE(SUM(fee),0) f "
            "FROM withdraws WHERE status='paid'"
        )).fetchone()
        pending_withdraws = (await (await conn.execute(
            "SELECT COUNT(*) c FROM withdraws WHERE status='pending'"
        )).fetchone())["c"]
        pending_products = (await (await conn.execute(
            "SELECT COUNT(*) c FROM products WHERE status='pending'"
        )).fetchone())["c"]
    commission = await get_commission_percent()
    text = (
        "<b>📊 Статистика</b>\n\n"
        f"Пользователей: <b>{users}</b>\n"
        f"Товаров всего: <b>{products_total}</b> (в каталоге: {products_active}, на модерации: {pending_products})\n"
        f"Продаж: <b>{sales['c']}</b> на <b>{esc(rub(sales['s']))}</b>\n"
        f"Пополнений: <b>{esc(rub(deposits_paid['s']))}</b>\n"
        f"Выплат: <b>{esc(rub(withdraws_paid['s']))}</b> (комиссия: <b>{esc(rub(withdraws_paid['f']))}</b>)\n"
        f"Заявок на вывод в ожидании: <b>{pending_withdraws}</b>\n"
        f"Текущая комиссия на вывод: <b>{commission}%</b>"
    )
    await c.message.edit_text(text, reply_markup=kb_back("admin:menu"))
    await c.answer()


# --- Рассылка --- #

@router.callback_query(F.data == "admin:broadcast")
async def cb_admin_broadcast(c: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(c.from_user.id):
        return
    await state.set_state(AdminSG.broadcast_text)
    await c.message.edit_text(
        "📣 Пришлите сообщение для рассылки (текст с HTML-разметкой или медиа с подписью).\n"
        "Сообщение будет отправлено как копия каждому пользователю.",
        reply_markup=kb_back("admin:menu"),
    )
    await c.answer()


@router.message(AdminSG.broadcast_text)
async def admin_broadcast_send(m: Message, state: FSMContext) -> None:
    if not is_admin(m.from_user.id):
        return
    await state.clear()
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (await conn.execute("SELECT tg_id FROM users")).fetchall()
    total = len(rows)
    sent, blocked, failed = 0, 0, 0
    await m.answer(f"Начинаю рассылку по {total} пользователям…")
    for r in rows:
        try:
            await bot.copy_message(r["tg_id"], m.chat.id, m.message_id)
            sent += 1
        except TelegramForbiddenError:
            blocked += 1
        except TelegramAPIError:
            failed += 1
        await asyncio.sleep(0.04)  # ~25 сообщений/с, анти-флуд
    await m.answer(
        f"Готово. Доставлено: <b>{sent}</b>, заблокировали бот: <b>{blocked}</b>, "
        f"ошибок: <b>{failed}</b>.",
        reply_markup=kb_admin_menu(),
    )


# --- Выдать баланс --- #

@router.callback_query(F.data == "admin:give")
async def cb_admin_give(c: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(c.from_user.id):
        return
    await state.set_state(AdminSG.give_balance_user)
    await c.message.edit_text(
        "💵 Введите <b>tg_id пользователя</b>, которому начислить/списать баланс:",
        reply_markup=kb_back("admin:menu"),
    )
    await c.answer()


@router.message(AdminSG.give_balance_user)
async def admin_give_user(m: Message, state: FSMContext) -> None:
    try:
        uid = int((m.text or "").strip())
    except ValueError:
        await m.answer("Некорректный id. Введите целое число.")
        return
    await state.update_data(give_user=uid)
    await state.set_state(AdminSG.give_balance_amount)
    await m.answer(
        "Введите сумму в рублях (положительная — начислить, отрицательная — списать):"
    )


@router.message(AdminSG.give_balance_amount)
async def admin_give_amount(m: Message, state: FSMContext) -> None:
    raw = (m.text or "").strip().replace(" ", "").replace(",", ".")
    try:
        val = float(raw)
    except ValueError:
        await m.answer("Некорректная сумма.")
        return
    kop = int(round(val * 100))
    data = await state.get_data()
    uid: int = int(data["give_user"])
    new_bal = await change_balance(uid, kop, "admin_credit", f"by admin {m.from_user.id}")
    with suppress(TelegramAPIError):
        await bot.send_message(
            uid,
            f"💵 Администратор изменил ваш баланс на <b>{esc(rub(kop))}</b>. "
            f"Текущий баланс: <b>{esc(rub(new_bal))}</b>.",
        )
    await state.clear()
    await m.answer(
        f"Готово. Баланс пользователя <code>{uid}</code>: <b>{esc(rub(new_bal))}</b>.",
        reply_markup=kb_admin_menu(),
    )


# --- Реквизиты --- #

@router.callback_query(F.data == "admin:req")
async def cb_admin_req(c: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(c.from_user.id):
        return
    current = await setting_get("requisites", "(не задано)")
    await state.set_state(AdminSG.set_requisites)
    await c.message.edit_text(
        f"<b>💳 Реквизиты для ручного перевода</b>\n\n"
        f"Текущие:\n<blockquote>{esc(current)}</blockquote>\n\n"
        f"Пришлите новые реквизиты одним сообщением (номер карты, ФИО, банк, ник):",
        reply_markup=kb_back("admin:menu"),
    )
    await c.answer()


@router.message(AdminSG.set_requisites)
async def admin_set_req(m: Message, state: FSMContext) -> None:
    text = (m.text or "").strip()
    if not text:
        await m.answer("Пусто, повторите.")
        return
    await setting_set("requisites", text)
    await state.clear()
    await m.answer("✅ Реквизиты обновлены.", reply_markup=kb_admin_menu())


# --- Товары (админ) --- #

@router.callback_query(F.data.startswith("admin:products:"))
async def cb_admin_products(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    page = int(c.data.split(":")[2])
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (await conn.execute(
            "SELECT id, title, price, status FROM products "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            (PAGE_SIZE, page * PAGE_SIZE),
        )).fetchall()
        total = (await (await conn.execute(
            "SELECT COUNT(*) c FROM products"
        )).fetchone())["c"]
    b = InlineKeyboardBuilder()
    for r in rows:
        emoji = {"pending": "⏳", "approved": "✅", "rejected": "❌", "archived": "📭"}.get(
            r["status"], "•"
        )
        b.button(
            text=f"{emoji} #{r['id']} {r['title'][:25]} — {rub(r['price'])}",
            callback_data=f"admin:product:{r['id']}",
        )
    b.adjust(1)
    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="⬅️", callback_data=f"admin:products:{page-1}")
    if (page + 1) * PAGE_SIZE < total:
        nav.button(text="➡️", callback_data=f"admin:products:{page+1}")
    nav.adjust(2)
    b.attach(nav)
    back = InlineKeyboardBuilder()
    back.button(text="⬅️ Админ", callback_data="admin:menu")
    b.attach(back)
    await c.message.edit_text(
        f"<b>📦 Все товары ({total})</b>", reply_markup=b.as_markup()
    )
    await c.answer()


@router.callback_query(F.data.startswith("admin:product:"))
async def cb_admin_product_view(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    pid = int(c.data.split(":")[2])
    rendered = await render_product_card(pid)
    if not rendered:
        await c.answer("Нет", show_alert=True)
        return
    text, p = rendered
    b = InlineKeyboardBuilder()
    if p["status"] == "pending":
        b.button(text="✅ Одобрить", callback_data=f"admin:prod_approve:{pid}")
        b.button(text="❌ Отклонить", callback_data=f"admin:prod_reject:{pid}")
    elif p["status"] == "approved":
        b.button(text="📭 Снять с продажи", callback_data=f"admin:prod_archive:{pid}")
    elif p["status"] in ("rejected", "archived"):
        b.button(text="✅ Вернуть в каталог", callback_data=f"admin:prod_restore:{pid}")
    b.button(text="💲 Изменить цену", callback_data=f"admin:prod_price:{pid}")
    b.button(text="🗑 Удалить", callback_data=f"admin:prod_delete:{pid}")
    b.button(text="⬅️ К списку", callback_data="admin:products:0")
    b.adjust(2, 2, 1)
    await c.message.edit_text(
        text + f"\n\nСтатус: <b>{esc(p['status'])}</b>",
        reply_markup=b.as_markup(),
    )
    await c.answer()


async def _set_product_status(pid: int, status: str) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE products SET status = ? WHERE id = ?", (status, pid)
        )
        await conn.commit()


@router.callback_query(F.data.startswith("admin:prod_approve:"))
async def cb_admin_prod_approve(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    pid = int(c.data.split(":")[2])
    await _set_product_status(pid, "approved")
    await c.answer("Одобрено")
    await cb_admin_product_view(c)


@router.callback_query(F.data.startswith("admin:prod_reject:"))
async def cb_admin_prod_reject(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    pid = int(c.data.split(":")[2])
    await _set_product_status(pid, "rejected")
    await c.answer("Отклонено")
    await cb_admin_product_view(c)


@router.callback_query(F.data.startswith("admin:prod_archive:"))
async def cb_admin_prod_archive(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    pid = int(c.data.split(":")[2])
    await _set_product_status(pid, "archived")
    await c.answer("Снято с продажи")
    await cb_admin_product_view(c)


@router.callback_query(F.data.startswith("admin:prod_restore:"))
async def cb_admin_prod_restore(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    pid = int(c.data.split(":")[2])
    await _set_product_status(pid, "approved")
    await c.answer("Возвращено в каталог")
    await cb_admin_product_view(c)


@router.callback_query(F.data.startswith("admin:prod_delete:"))
async def cb_admin_prod_delete(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    pid = int(c.data.split(":")[2])
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("DELETE FROM products WHERE id = ?", (pid,))
        await conn.commit()
    await c.message.edit_text(
        f"🗑 Товар #{pid} удалён.", reply_markup=kb_back("admin:products:0")
    )
    await c.answer("Удалено")


@router.callback_query(F.data.startswith("admin:prod_price:"))
async def cb_admin_prod_price(c: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(c.from_user.id):
        return
    pid = int(c.data.split(":")[2])
    await state.set_state(AdminSG.edit_product_price)
    await state.update_data(edit_pid=pid)
    await c.message.edit_text(
        f"Введите новую цену товара #{pid} в рублях:",
        reply_markup=kb_back(f"admin:product:{pid}"),
    )
    await c.answer()


@router.message(AdminSG.edit_product_price)
async def admin_prod_price_set(m: Message, state: FSMContext) -> None:
    kop = parse_rub_to_kopecks(m.text or "")
    if kop is None or kop <= 0:
        await m.answer("Некорректная цена.")
        return
    data = await state.get_data()
    pid = int(data["edit_pid"])
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE products SET price = ? WHERE id = ?", (kop, pid)
        )
        await conn.commit()
    await state.clear()
    await m.answer(
        f"✅ Цена товара #{pid} обновлена: <b>{esc(rub(kop))}</b>.",
        reply_markup=kb_admin_menu(),
    )


# --- Комиссия --- #

@router.callback_query(F.data == "admin:commission")
async def cb_admin_commission(c: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(c.from_user.id):
        return
    current = await get_commission_percent()
    await state.set_state(AdminSG.set_commission)
    await c.message.edit_text(
        f"⚙️ Текущая комиссия на вывод: <b>{current}%</b>\n\n"
        f"Введите новое значение (0–100):",
        reply_markup=kb_back("admin:menu"),
    )
    await c.answer()


@router.message(AdminSG.set_commission)
async def admin_set_commission(m: Message, state: FSMContext) -> None:
    try:
        val = int((m.text or "").strip())
    except ValueError:
        await m.answer("Нужно целое число 0..100.")
        return
    if not 0 <= val <= 100:
        await m.answer("Значение должно быть 0..100.")
        return
    await setting_set("commission_percent", str(val))
    await state.clear()
    await m.answer(
        f"✅ Комиссия на вывод обновлена: <b>{val}%</b>.",
        reply_markup=kb_admin_menu(),
    )


# --------------------------------------------------------------------------- #
# Fallback — подчистка неизвестного ввода
# --------------------------------------------------------------------------- #

@router.message(StateFilter(None))
async def fallback_any(m: Message) -> None:
    # Игнорируем всё, что не /start и /admin — чтобы не засорять чат
    if m.text and m.text.startswith("/"):
        return
    await m.answer(
        "Используйте меню или команды /start и /admin.",
        reply_markup=kb_main(is_admin(m.from_user.id)),
    )


# --------------------------------------------------------------------------- #
# Точка входа
# --------------------------------------------------------------------------- #

async def main() -> None:
    await init_db()
    await set_bot_commands()
    log.info("Bot is starting…")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped.")
