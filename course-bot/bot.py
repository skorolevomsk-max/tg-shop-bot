"""
Telegram-бот: школа курсов (напр. маникюр) — продажа курсов с уроками.

Стек: Python 3.10+, aiogram 3.26.0, SQLite (aiosqlite), HTML-разметка.
Платежи: CryptoBot (Crypto Pay API) + ручной перевод по реквизитам.
Фичи: промокоды на скидку, реферальная программа с % продавцу.

Запуск: python bot.py   (см. .env.example)
"""

from __future__ import annotations

import asyncio
import base64
import csv
import datetime as dt
import html
import io
import json
import logging
import os
import secrets as pysecrets
import string
import uuid
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
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyParameters,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:  # pragma: no cover
    PIL_AVAILABLE = False

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

# YooKassa (опционально). Пусто — способ оплаты скрыт.
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "").strip()
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "").strip()

# Telegram Stars. Курс: сколько ⭐️ за 1 ₽ (округляется вверх). По умолчанию 2 stars / 10 ₽.
STARS_ENABLED = os.getenv("STARS_ENABLED", "true").lower() == "true"
try:
    STARS_PER_RUB = float(os.getenv("STARS_PER_RUB", "0.2"))
except ValueError:
    STARS_PER_RUB = 0.2


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

CREATE TABLE IF NOT EXISTS categories (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT    NOT NULL UNIQUE,
    position INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS lesson_progress (
    user_id     INTEGER NOT NULL,
    lesson_id   INTEGER NOT NULL,
    completed_at TEXT   NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, lesson_id)
);

CREATE TABLE IF NOT EXISTS reviews (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    course_id  INTEGER NOT NULL,
    stars      INTEGER NOT NULL,
    text       TEXT    NOT NULL DEFAULT '',
    status     TEXT    NOT NULL DEFAULT 'pending',  -- pending/approved/rejected
    moderation_msg_id INTEGER,
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, course_id)
);

CREATE TABLE IF NOT EXISTS homeworks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    lesson_id       INTEGER NOT NULL,
    content_type    TEXT    NOT NULL,
    content_file_id TEXT,
    content_text    TEXT,
    caption         TEXT,
    status          TEXT    NOT NULL DEFAULT 'pending', -- pending/approved/revise
    reviewer_note   TEXT,
    moderation_msg_id INTEGER,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    reviewed_at     TEXT
);

CREATE TABLE IF NOT EXISTS certificates (
    user_id    INTEGER NOT NULL,
    course_id  INTEGER NOT NULL,
    file_id    TEXT,
    issued_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, course_id)
);

CREATE TABLE IF NOT EXISTS news (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT    NOT NULL,
    body            TEXT    NOT NULL DEFAULT '',
    media_type      TEXT,                                -- photo/video/animation/document
    media_file_id   TEXT,
    is_published    INTEGER NOT NULL DEFAULT 0,
    is_pinned       INTEGER NOT NULL DEFAULT 0,
    broadcast_done  INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""


_MIGRATIONS: list[tuple[str, str, str]] = [
    # (table, column, ddl-fragment after "ADD COLUMN")
    ("courses", "category_id",    "INTEGER"),
    ("courses", "chat_id",        "INTEGER"),              # закрытый чат курса
    ("courses", "star_price",     "INTEGER"),              # опц. фикс. цена в Stars; null = авто-курс
    ("lessons", "has_homework",   "INTEGER NOT NULL DEFAULT 0"),
    ("deposits","method_extra",   "TEXT"),                 # напр. yookassa payment_id
]


async def _run_migrations(conn: aiosqlite.Connection) -> None:
    conn.row_factory = aiosqlite.Row
    for table, col, ddl in _MIGRATIONS:
        cols = await (await conn.execute(f"PRAGMA table_info({table})")).fetchall()
        if any(c["name"] == col for c in cols):
            continue
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.executescript(SCHEMA)
        await _run_migrations(conn)
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
        await conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
            ("school_name", "Школа маникюра"),
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
# YooKassa API
# --------------------------------------------------------------------------- #

class YooKassa:
    def __init__(self, shop_id: str, secret_key: str) -> None:
        self.shop_id = shop_id
        self.secret_key = secret_key
        self.base = "https://api.yookassa.ru/v3"

    @property
    def enabled(self) -> bool:
        return bool(self.shop_id and self.secret_key)

    def _auth(self) -> aiohttp.BasicAuth:
        return aiohttp.BasicAuth(self.shop_id, self.secret_key)

    async def create_payment(self, amount_rub: float, description: str,
                             return_url: str) -> dict[str, Any]:
        idem = str(uuid.uuid4())
        headers = {
            "Idempotence-Key": idem,
            "Content-Type": "application/json",
        }
        payload = {
            "amount": {"value": f"{amount_rub:.2f}", "currency": "RUB"},
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": return_url},
            "description": description[:128],
        }
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout, auth=self._auth()) as sess:
            async with sess.post(f"{self.base}/payments",
                                 headers=headers, data=json.dumps(payload)) as r:
                data = await r.json()
                if r.status >= 400:
                    raise RuntimeError(f"YooKassa error {r.status}: {data}")
        return data

    async def get_payment(self, payment_id: str) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout, auth=self._auth()) as sess:
            async with sess.get(f"{self.base}/payments/{payment_id}") as r:
                return await r.json()


yookassa = YooKassa(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)


# --------------------------------------------------------------------------- #
# Сертификаты (PDF через Pillow)
# --------------------------------------------------------------------------- #

def _make_certificate_pdf(user_name: str, course_title: str,
                          school_name: str) -> Optional[bytes]:
    if not PIL_AVAILABLE:
        return None
    # A4 landscape, 300 dpi = 3508 x 2480 (ландшафт 3508x2480)
    W, H = 2480, 1754
    img = Image.new("RGB", (W, H), (250, 247, 240))
    draw = ImageDraw.Draw(img)

    # рамка
    border_thickness = 24
    draw.rectangle([60, 60, W - 60, H - 60], outline=(174, 110, 56), width=border_thickness)
    draw.rectangle([120, 120, W - 120, H - 120], outline=(210, 170, 95), width=4)

    font_dir = "/usr/share/fonts/truetype/dejavu"
    try:
        f_title = ImageFont.truetype(f"{font_dir}/DejaVuSerif-Bold.ttf", 160)
        f_sub = ImageFont.truetype(f"{font_dir}/DejaVuSerif.ttf", 70)
        f_name = ImageFont.truetype(f"{font_dir}/DejaVuSerif-Bold.ttf", 130)
        f_body = ImageFont.truetype(f"{font_dir}/DejaVuSerif.ttf", 64)
        f_small = ImageFont.truetype(f"{font_dir}/DejaVuSans.ttf", 48)
    except OSError:
        f_title = f_sub = f_name = f_body = f_small = ImageFont.load_default()

    def center_text(y: int, text: str, font: ImageFont.FreeTypeFont,
                    color: tuple[int, int, int] = (40, 25, 10)) -> None:
        bbox = draw.textbbox((0, 0), text, font=font)
        w = bbox[2] - bbox[0]
        draw.text(((W - w) // 2, y), text, font=font, fill=color)

    center_text(260, "СЕРТИФИКАТ", f_title, (140, 85, 40))
    center_text(460, "об успешном прохождении курса", f_sub, (90, 60, 30))
    center_text(700, user_name[:60], f_name, (40, 25, 10))
    center_text(900, "успешно завершил(а) курс", f_body, (90, 60, 30))
    center_text(1030, f"«{course_title[:55]}»", f_body, (40, 25, 10))

    center_text(1350, school_name[:80], f_sub, (140, 85, 40))
    date_str = dt.datetime.now().strftime("%d.%m.%Y")
    center_text(1470, f"Дата выдачи: {date_str}", f_small, (90, 60, 30))

    buf = io.BytesIO()
    img.save(buf, format="PDF", resolution=300)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Helpers: progress, reviews, stars
# --------------------------------------------------------------------------- #

def rub_to_stars(rub_kopecks: int) -> int:
    """Переводит цену в копейках в целое число Stars. Минимум 1."""
    rubles = rub_kopecks / 100
    import math
    return max(1, math.ceil(rubles * STARS_PER_RUB))


async def course_rating(course_id: int) -> tuple[float, int]:
    """Возвращает (средняя_звёзда, количество_отзывов) по одобренным отзывам."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        row = await (await conn.execute(
            "SELECT AVG(stars) AS avg, COUNT(*) AS c FROM reviews "
            "WHERE course_id = ? AND status = 'approved'",
            (course_id,),
        )).fetchone()
    if not row or not row["c"]:
        return 0.0, 0
    return float(row["avg"] or 0), int(row["c"])


async def course_progress(user_id: int, course_id: int) -> tuple[int, int]:
    """Возвращает (пройдено_уроков, всего_уроков)."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        total = (await (await conn.execute(
            "SELECT COUNT(*) AS c FROM lessons WHERE course_id = ?", (course_id,)
        )).fetchone())["c"]
        done = (await (await conn.execute(
            "SELECT COUNT(*) AS c FROM lesson_progress lp "
            "JOIN lessons l ON l.id = lp.lesson_id "
            "WHERE lp.user_id = ? AND l.course_id = ?",
            (user_id, course_id),
        )).fetchone())["c"]
    return done, total


async def mark_lesson_completed(user_id: int, lesson_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO lesson_progress(user_id, lesson_id) VALUES (?, ?)",
            (user_id, lesson_id),
        )
        await conn.commit()


async def homework_status_for(user_id: int, lesson_id: int) -> Optional[str]:
    """None если ДЗ не сдавалось, иначе 'pending'/'approved'/'revise' (последняя отправка)."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        row = await (await conn.execute(
            "SELECT status FROM homeworks WHERE user_id = ? AND lesson_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (user_id, lesson_id),
        )).fetchone()
    return row["status"] if row else None


def progress_bar(done: int, total: int, width: int = 10) -> str:
    if total <= 0:
        return ""
    filled = int(round(done / total * width))
    return "▓" * filled + "░" * (width - filled)


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
    set_school_name = State()
    give_user = State()
    give_amount = State()
    grant_user = State()
    promo_code = State()
    promo_discount = State()
    promo_limit = State()
    category_name = State()
    category_rename = State()
    course_chat_id = State()
    course_star_price = State()
    homework_note = State()


class BuySG(StatesGroup):
    promo = State()
    manual_proof = State()


class WithdrawSG(StatesGroup):
    amount = State()
    card = State()
    fio = State()
    bank = State()


class ReviewSG(StatesGroup):
    stars = State()
    text = State()


class HomeworkSG(StatesGroup):
    content = State()


class NewsSG(StatesGroup):
    title = State()
    body = State()
    media = State()
    edit_value = State()


# --------------------------------------------------------------------------- #
# Клавиатуры
# --------------------------------------------------------------------------- #

def kb_main(is_admin_user: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📚 Каталог курсов", callback_data="catalog:0")
    b.button(text="🎓 Мои курсы", callback_data="my_courses")
    b.button(text="📰 Новости", callback_data="news:0")
    b.button(text="👤 Профиль", callback_data="profile")
    b.button(text="🎁 Реф. программа", callback_data="ref")
    if is_admin_user:
        b.button(text="🛠 Админ-панель", callback_data="admin:menu")
    b.adjust(2, 1, 2, 1)
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
    b.button(text="🗂 Категории", callback_data="admin:cats")
    b.button(text="⭐️ Отзывы", callback_data="admin:reviews:0")
    b.button(text="📝 Домашки", callback_data="admin:hw:0")
    b.button(text="📣 Рассылка", callback_data="admin:broadcast")
    b.button(text="💳 Реквизиты", callback_data="admin:req")
    b.button(text="🎁 Реф. комиссия", callback_data="admin:ref_pct")
    b.button(text="👤 Выдать курс", callback_data="admin:grant")
    b.button(text="💵 Выдать баланс", callback_data="admin:give")
    b.button(text="📝 Приветствие", callback_data="admin:welcome")
    b.button(text="🏫 Название школы", callback_data="admin:school")
    b.button(text="📥 Экспорт CSV", callback_data="admin:export")
    b.button(text="💾 Бэкап БД", callback_data="admin:backup")
    b.button(text="📰 Новости", callback_data="admin:news:0")
    b.button(text="⬅️ В меню", callback_data="main")
    b.adjust(2, 2, 2, 2, 2, 2, 2, 2, 1, 1)
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
    # формат: catalog:<page>[:<cat_id|all>]
    parts = c.data.split(":")
    page = int(parts[1])
    cat_filter = parts[2] if len(parts) >= 3 else "all"
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        # список категорий, у которых есть опубликованные курсы
        cats = await (await conn.execute(
            "SELECT c.id, c.name, COUNT(co.id) AS cnt FROM categories c "
            "LEFT JOIN courses co ON co.category_id = c.id AND co.is_published = 1 "
            "GROUP BY c.id ORDER BY c.position, c.id"
        )).fetchall()

        where = "is_published = 1"
        params: list[Any] = []
        if cat_filter == "none":
            where += " AND category_id IS NULL"
        elif cat_filter != "all":
            where += " AND category_id = ?"
            params.append(int(cat_filter))

        rows = await (await conn.execute(
            f"SELECT id, title, price FROM courses WHERE {where} "
            f"ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, PAGE_SIZE, page * PAGE_SIZE),
        )).fetchall()
        total = (await (await conn.execute(
            f"SELECT COUNT(*) AS c FROM courses WHERE {where}", params
        )).fetchone())["c"]

    b = InlineKeyboardBuilder()
    # блок фильтра по категориям
    if cats:
        filt = InlineKeyboardBuilder()
        prefix_all = "✅ " if cat_filter == "all" else ""
        filt.button(text=f"{prefix_all}Все", callback_data="catalog:0:all")
        for cat in cats:
            if not cat["cnt"]:
                continue
            prefix = "✅ " if cat_filter == str(cat["id"]) else ""
            filt.button(
                text=f"{prefix}{cat['name'][:22]}",
                callback_data=f"catalog:0:{cat['id']}",
            )
        filt.adjust(3)
        b.attach(filt)

    if not rows and page == 0:
        b2 = InlineKeyboardBuilder()
        b2.button(text="⬅️ В меню", callback_data="main")
        b.attach(b2)
        await _render_catalog_text(c,
            "📭 Курсы пока не опубликованы. Зайдите позже.", b.as_markup())
        await c.answer()
        return

    for r in rows:
        avg, cnt = await course_rating(r["id"])
        stars = f" ⭐{avg:.1f}" if cnt else ""
        b.button(
            text=f"{r['title'][:36]} — {rub(r['price'])}{stars}",
            callback_data=f"course:{r['id']}",
        )
    b.adjust(3, 1)  # 3 в строке категорий, потом по 1 курс
    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="⬅️", callback_data=f"catalog:{page-1}:{cat_filter}")
    if (page + 1) * PAGE_SIZE < total:
        nav.button(text="➡️", callback_data=f"catalog:{page+1}:{cat_filter}")
    if nav.buttons:
        nav.adjust(2)
        b.attach(nav)
    back = InlineKeyboardBuilder()
    back.button(text="⬅️ В меню", callback_data="main")
    b.attach(back)
    cat_label = "все"
    if cat_filter != "all" and cat_filter != "none":
        cat_row = next((c2 for c2 in cats if str(c2["id"]) == cat_filter), None)
        if cat_row:
            cat_label = cat_row["name"]
    elif cat_filter == "none":
        cat_label = "без категории"
    text = (
        f"<b>📚 Каталог курсов</b>\n"
        f"Категория: <b>{esc(cat_label)}</b>\n"
        f"Найдено: <b>{total}</b>"
    )
    await _render_catalog_text(c, text, b.as_markup())
    await c.answer()


async def _render_catalog_text(c: CallbackQuery, text: str,
                                kb: InlineKeyboardMarkup) -> None:
    # caption (если сообщение было с медиа) или edit_text
    try:
        if c.message.caption is not None:
            await c.message.delete()
            await bot.send_message(c.message.chat.id, text, reply_markup=kb)
        else:
            await c.message.edit_text(text, reply_markup=kb)
    except TelegramAPIError:
        await bot.send_message(c.message.chat.id, text, reply_markup=kb)


async def _render_course(cid: int,
                         viewer_id: Optional[int] = None
                         ) -> Optional[tuple[str, aiosqlite.Row, list[aiosqlite.Row]]]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        course = await (await conn.execute(
            "SELECT c.*, cat.name AS cat_name FROM courses c "
            "LEFT JOIN categories cat ON cat.id = c.category_id "
            "WHERE c.id = ?",
            (cid,),
        )).fetchone()
        if not course:
            return None
        lessons = await (await conn.execute(
            "SELECT id, title, is_free, position, has_homework FROM lessons "
            "WHERE course_id = ? ORDER BY position, id",
            (cid,),
        )).fetchall()
    free_cnt = sum(1 for l in lessons if l["is_free"])
    avg, rev_cnt = await course_rating(cid)
    rating_line = ""
    if rev_cnt:
        rating_line = f"\n⭐ Рейтинг: <b>{avg:.1f}</b> ({rev_cnt})"
    cat_line = ""
    if course["cat_name"]:
        cat_line = f"\n🗂 Категория: <b>{esc(course['cat_name'])}</b>"
    progress_line = ""
    if viewer_id is not None and lessons:
        done, total = await course_progress(viewer_id, cid)
        if total and done:
            bar = progress_bar(done, total)
            pct = done * 100 // total if total else 0
            progress_line = f"\n📈 Прогресс: {bar} {pct}% ({done}/{total})"

    text = (
        f"<b>{esc(course['title'])}</b>"
        f"{cat_line}{rating_line}{progress_line}\n\n"
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
    rendered = await _render_course(cid, viewer_id=c.from_user.id)
    if not rendered:
        await c.answer("Курс не найден", show_alert=True)
        return
    text, course, lessons = rendered
    owned = await has_course(c.from_user.id, cid)

    # статус прохождения
    progress_set: set[int] = set()
    if owned and lessons:
        async with aiosqlite.connect(DB_PATH) as conn:
            conn.row_factory = aiosqlite.Row
            done_rows = await (await conn.execute(
                "SELECT lesson_id FROM lesson_progress lp "
                "JOIN lessons l ON l.id = lp.lesson_id "
                "WHERE lp.user_id = ? AND l.course_id = ?",
                (c.from_user.id, cid),
            )).fetchall()
            progress_set = {r["lesson_id"] for r in done_rows}

    b = InlineKeyboardBuilder()
    # Уроки как кнопки
    for l in lessons:
        locked = not (owned or l["is_free"] or is_admin(c.from_user.id))
        if locked:
            label = f"🔒 {l['title'][:38]}"
        elif l["id"] in progress_set:
            label = f"✅ {l['title'][:38]}"
        elif l["is_free"] and not owned:
            label = f"🆓 {l['title'][:38]}"
        else:
            label = f"▶️ {l['title'][:38]}"
        if l["has_homework"]:
            label += " 📝"
        b.button(text=label, callback_data=f"lesson:{l['id']}")
    b.adjust(1)

    action = InlineKeyboardBuilder()
    if owned:
        action.button(text="✅ Курс куплен", callback_data="noop")
        # закрытый чат курса
        if course["chat_id"]:
            action.button(text="💬 Чат курса", callback_data=f"chat:{cid}")
        # отзыв
        action.button(text="⭐ Оставить отзыв", callback_data=f"review:{cid}")
        # сертификат (если все уроки пройдены)
        if lessons and len(progress_set) >= len(lessons):
            action.button(text="🏆 Сертификат (PDF)", callback_data=f"cert:{cid}")
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

    # статус урока
    done = await _lesson_is_completed(c.from_user.id, lesson["id"])
    hw_status = await homework_status_for(c.from_user.id, lesson["id"]) if lesson["has_homework"] else None

    # Посылаем урок отдельным сообщением
    status_line = ""
    if owned:
        if done:
            status_line = "\n✅ <i>Урок пройден</i>"
        elif lesson["has_homework"]:
            status_line = {
                None:       "\n📝 <i>Требуется домашка</i>",
                "pending":  "\n⏳ <i>Домашка на проверке</i>",
                "revise":   "\n🔄 <i>Домашку нужно переделать</i>",
                "approved": "\n📝 <i>Домашка принята. Отметьте урок пройденным.</i>",
            }.get(hw_status, "")
    header = (
        f"<b>{esc(lesson['title'])}</b>{status_line}\n\n"
        + (esc(lesson["description"]) + "\n\n" if lesson["description"] else "")
    )
    ct = lesson["content_type"]
    fid = lesson["content_file_id"]
    caption = lesson["content_caption"] or ""
    combined_caption = (header + caption).strip()[:1024]

    b = InlineKeyboardBuilder()
    if owned and not done:
        can_complete = (not lesson["has_homework"]) or hw_status == "approved"
        if can_complete:
            b.button(text="✅ Отметить пройденным",
                     callback_data=f"done:{lesson['id']}")
        if lesson["has_homework"]:
            label = "📝 Сдать домашку" if hw_status in (None, "revise") else "📝 Моя домашка"
            b.button(text=label, callback_data=f"hw:{lesson['id']}")
    b.button(text="⬅️ К курсу", callback_data=f"course:{course_id}")
    b.adjust(1)

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
    if yookassa.enabled:
        b.button(text="🏦 Оплатить YooKassa", callback_data=f"buy:yk:{cid}")
    if STARS_ENABLED:
        stars_price = course["star_price"] or rub_to_stars(course["price"])
        b.button(text=f"⭐️ Оплатить Stars ({stars_price})",
                 callback_data=f"buy:stars:{cid}")
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
    if yookassa.enabled:
        b.button(text=f"🏦 YooKassa — {rub(price)}", callback_data=f"buy:yk:{cid}")
    if STARS_ENABLED:
        stars = rub_to_stars(price)
        b.button(text=f"⭐️ Stars ({stars})", callback_data=f"buy:stars:{cid}")
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

    course = await _get_course(int(dep["course_id"]))
    with suppress(TelegramAPIError):
        await bot.send_message(
            int(dep["user_id"]),
            f"✅ Оплата подтверждена! Курс <b>{esc(course['title'] if course else '')}</b> "
            f"открыт.",
            reply_markup=kb_back("main"),
        )

    # уведомление администратора(ов) о каждой успешной оплате
    await _notify_admins_purchase(
        user_id=int(dep["user_id"]),
        course_title=(course["title"] if course else f"#{dep['course_id']}"),
        amount_kop=int(dep["amount"]),
        method=str(dep["method"]),
        promo_code=dep["promo_code"],
    )

    # пост-покупка: выдать invite в закрытый чат курса, если настроен
    await _post_purchase_actions(int(dep["user_id"]), int(dep["course_id"]))


_METHOD_LABELS = {
    "manual": "💳 Перевод на карту",
    "crypto": "🤖 CryptoBot",
    "yookassa": "🏦 YooKassa",
    "stars": "⭐️ Telegram Stars",
    "balance": "💼 С баланса",
    "admin_grant": "👤 Выдан админом",
}


async def _notify_admins_purchase(*, user_id: int, course_title: str,
                                  amount_kop: int, method: str,
                                  promo_code: Optional[str]) -> None:
    if not ADMIN_IDS:
        return
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        u = await (await conn.execute(
            "SELECT username, full_name FROM users WHERE tg_id = ?", (user_id,)
        )).fetchone()
    name = (u["full_name"] if u and u["full_name"] else f"#{user_id}")
    uname = f"@{u['username']}" if u and u["username"] else ""
    method_label = _METHOD_LABELS.get(method, method)
    promo_line = f"\n🏷 Промокод: <code>{esc(promo_code)}</code>" if promo_code else ""
    text = (
        "💰 <b>Новая оплата</b>\n"
        f"👤 {esc(name)} {esc(uname)} (<code>{user_id}</code>)\n"
        f"📚 Курс: <b>{esc(course_title)}</b>\n"
        f"💵 Сумма: <b>{rub(amount_kop)}</b>\n"
        f"💳 Метод: {method_label}"
        f"{promo_line}"
    )
    for aid in ADMIN_IDS:
        with suppress(TelegramAPIError):
            await bot.send_message(aid, text)


async def _post_purchase_actions(user_id: int, course_id: int) -> None:
    course = await _get_course(course_id)
    if not course:
        return
    # 1) invite в закрытый чат курса
    chat_id = course["chat_id"]
    if chat_id:
        try:
            link = await bot.create_chat_invite_link(
                chat_id=int(chat_id),
                member_limit=1,
                expire_date=dt.datetime.now() + dt.timedelta(hours=24),
                name=f"course-{course_id}-uid-{user_id}",
            )
            with suppress(TelegramAPIError):
                await bot.send_message(
                    user_id,
                    f"💬 Ваша персональная ссылка на закрытый чат курса "
                    f"<b>{esc(course['title'])}</b> (действует 24 часа, "
                    f"одноразовая):\n\n{link.invite_link}",
                )
        except TelegramAPIError as e:
            log.warning("failed to create invite link: %s", e)
            with suppress(TelegramAPIError):
                for aid in ADMIN_IDS:
                    await bot.send_message(
                        aid,
                        f"⚠️ Не удалось сгенерировать invite link для чата курса "
                        f"«{esc(course['title'])}» (chat_id={chat_id}): {esc(str(e))}.\n"
                        f"Проверьте, что бот — админ в этом чате с правом приглашать.",
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


# --- Ученики курса (с прогрессом) --- #

@router.callback_query(F.data.startswith("admin:students:"))
async def cb_admin_students(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    parts = c.data.split(":")
    cid = int(parts[2])
    page = int(parts[3]) if len(parts) > 3 else 0

    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        course = await (await conn.execute(
            "SELECT title FROM courses WHERE id = ?", (cid,)
        )).fetchone()
        total_lessons = (await (await conn.execute(
            "SELECT COUNT(*) AS c FROM lessons WHERE course_id = ?", (cid,)
        )).fetchone())["c"]
        total = (await (await conn.execute(
            "SELECT COUNT(*) AS c FROM purchases WHERE course_id = ?", (cid,)
        )).fetchone())["c"]
        rows = await (await conn.execute(
            "SELECT p.user_id, p.created_at, p.price, "
            "       u.username, u.full_name "
            "FROM purchases p LEFT JOIN users u ON u.tg_id = p.user_id "
            "WHERE p.course_id = ? "
            "ORDER BY p.created_at DESC LIMIT ? OFFSET ?",
            (cid, PAGE_SIZE, page * PAGE_SIZE),
        )).fetchall()
        # прогресс по уроки этого курса для каждого ученика — одним запросом
        done_map: dict[int, int] = {}
        if rows:
            uids = [int(r["user_id"]) for r in rows]
            placeholders = ",".join(["?"] * len(uids))
            done_rows = await (await conn.execute(
                f"SELECT lp.user_id, COUNT(*) AS c "
                f"FROM lesson_progress lp "
                f"JOIN lessons l ON l.id = lp.lesson_id "
                f"WHERE l.course_id = ? AND lp.user_id IN ({placeholders}) "
                f"GROUP BY lp.user_id",
                (cid, *uids),
            )).fetchall()
            done_map = {int(r["user_id"]): int(r["c"]) for r in done_rows}

    if not course:
        await c.answer("Курс не найден", show_alert=True)
        return
    title = esc(course["title"])
    if total == 0:
        text = f"<b>👥 Ученики курса «{title}»</b>\n\nЕщё никто не купил."
    else:
        lines = [f"<b>👥 Ученики курса «{title}»</b>",
                 f"Всего: <b>{total}</b>, уроков: <b>{total_lessons}</b>\n"]
        for r in rows:
            uid = int(r["user_id"])
            done = done_map.get(uid, 0)
            pct = (100 * done // total_lessons) if total_lessons else 0
            name = r["full_name"] or (f"@{r['username']}" if r["username"]
                                      else f"#{uid}")
            uname = f" @{r['username']}" if r["username"] and r["full_name"] else ""
            created = (r["created_at"] or "")[:16].replace("T", " ")
            lines.append(
                f"• <b>{esc(name)}</b>{esc(uname)} (<code>{uid}</code>) — "
                f"{done}/{total_lessons} ({pct}%) — {esc(created)}"
            )
        text = "\n".join(lines)

    b = InlineKeyboardBuilder()
    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="⬅️", callback_data=f"admin:students:{cid}:{page-1}")
    if (page + 1) * PAGE_SIZE < total:
        nav.button(text="➡️", callback_data=f"admin:students:{cid}:{page+1}")
    nav.adjust(2)
    b.attach(nav)
    tail = InlineKeyboardBuilder()
    tail.button(text="⬅️ К курсу", callback_data=f"admin:course:{cid}")
    tail.adjust(1)
    b.attach(tail)
    with suppress(TelegramAPIError):
        await c.message.edit_text(text, reply_markup=b.as_markup())
    await c.answer()


# --- CSV-экспорт продаж --- #

@router.callback_query(F.data == "admin:export")
async def cb_admin_export(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    await c.answer("Готовлю CSV…")
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (await conn.execute(
            "SELECT p.id, p.created_at, p.user_id, "
            "       u.username, u.full_name, "
            "       p.course_id, c.title AS course_title, "
            "       p.price, p.method, p.promo_code "
            "FROM purchases p "
            "LEFT JOIN users   u ON u.tg_id = p.user_id "
            "LEFT JOIN courses c ON c.id    = p.course_id "
            "ORDER BY p.created_at DESC"
        )).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow([
        "id", "created_at", "user_id", "username", "full_name",
        "course_id", "course_title", "price_rub", "method", "promo_code",
    ])
    for r in rows:
        price_rub = f"{int(r['price']) / 100:.2f}".replace(".", ",")
        w.writerow([
            r["id"], r["created_at"], r["user_id"],
            r["username"] or "", r["full_name"] or "",
            r["course_id"], r["course_title"] or "",
            price_rub, r["method"], r["promo_code"] or "",
        ])
    data = buf.getvalue().encode("utf-8-sig")  # BOM, чтобы Excel/Numbers корректно открыл
    fname = f"purchases-{dt.datetime.now().strftime('%Y%m%d-%H%M')}.csv"
    with suppress(TelegramAPIError):
        await bot.send_document(
            c.from_user.id,
            BufferedInputFile(data, filename=fname),
            caption=(
                f"📥 Экспорт продаж — <b>{len(rows)}</b> строк.\n"
                f"Разделитель «;», кодировка UTF-8 BOM."
            ),
        )


# --- Бэкап БД --- #

@router.callback_query(F.data == "admin:backup")
async def cb_admin_backup(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    await c.answer("Делаю бэкап…")
    if not os.path.exists(DB_PATH):
        with suppress(TelegramAPIError):
            await bot.send_message(c.from_user.id, "❌ Файл БД не найден")
        return
    # SQLite VACUUM INTO даёт целостный snapshot без блокировки записи
    bkp_dir = os.path.dirname(DB_PATH) or "."
    bkp_path = os.path.join(
        bkp_dir, f"course-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    )
    try:
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(f"VACUUM INTO '{bkp_path}'")
        with open(bkp_path, "rb") as fh:
            data = fh.read()
        size_mb = len(data) / 1024 / 1024
        with suppress(TelegramAPIError):
            await bot.send_document(
                c.from_user.id,
                BufferedInputFile(data, filename=os.path.basename(bkp_path)),
                caption=f"💾 Бэкап БД ({size_mb:.2f} MiB)",
            )
    except Exception as e:
        log.exception("backup failed")
        with suppress(TelegramAPIError):
            await bot.send_message(c.from_user.id, f"❌ Ошибка бэкапа: {esc(str(e))}")
    finally:
        with suppress(Exception):
            if os.path.exists(bkp_path):
                os.remove(bkp_path)


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
    await _post_purchase_actions(uid, cid)
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
    b.button(text="🗂 Категория", callback_data=f"admin:course_cat:{cid}")
    b.button(text="💬 Закрытый чат", callback_data=f"admin:course_chat:{cid}")
    b.button(text="⭐️ Цена в Stars", callback_data=f"admin:course_star:{cid}")
    b.button(text="➕ Добавить урок", callback_data=f"admin:lesson_new:{cid}")
    if lessons:
        b.button(text="📋 Уроки", callback_data=f"admin:lessons:{cid}")
    b.button(text="👥 Ученики", callback_data=f"admin:students:{cid}:0")
    b.button(text="🗑 Удалить курс", callback_data=f"admin:course_del:{cid}")
    b.button(text="⬅️ К списку", callback_data="admin:courses:0")
    b.adjust(1, 2, 2, 2, 2, 1, 1)
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
        f"Требует домашки: <b>{'да' if lesson['has_homework'] else 'нет'}</b>\n"
        f"Позиция: <b>{lesson['position']}</b>"
    )
    b = InlineKeyboardBuilder()
    b.button(text="🆓/🔒 Перекл. доступ", callback_data=f"admin:lesson_togfree:{lid}")
    b.button(text="📝 Перекл. домашку", callback_data=f"admin:lesson_toghw:{lid}")
    b.button(text="⬆️ Вверх", callback_data=f"admin:lesson_up:{lid}")
    b.button(text="⬇️ Вниз", callback_data=f"admin:lesson_down:{lid}")
    b.button(text="✏️ Переименовать", callback_data=f"admin:lesson_edit:{lid}:title")
    b.button(text="📝 Описание", callback_data=f"admin:lesson_edit:{lid}:description")
    b.button(text="🗑 Удалить", callback_data=f"admin:lesson_del:{lid}")
    b.button(text="⬅️ К урокам", callback_data=f"admin:lessons:{lesson['course_id']}")
    b.adjust(2, 2, 2, 1, 1)
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
# Прогресс, сертификат, отзыв, чат
# --------------------------------------------------------------------------- #

async def _lesson_is_completed(user_id: int, lesson_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        row = await (await conn.execute(
            "SELECT 1 FROM lesson_progress WHERE user_id = ? AND lesson_id = ?",
            (user_id, lesson_id),
        )).fetchone()
    return bool(row)


@router.callback_query(F.data.startswith("done:"))
async def cb_lesson_done(c: CallbackQuery) -> None:
    lid = int(c.data.split(":")[1])
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        lesson = await (await conn.execute(
            "SELECT * FROM lessons WHERE id = ?", (lid,)
        )).fetchone()
    if not lesson:
        await c.answer("Урок не найден", show_alert=True)
        return
    if not await has_course(c.from_user.id, lesson["course_id"]):
        await c.answer("Урок не куплен", show_alert=True)
        return
    if lesson["has_homework"]:
        hw = await homework_status_for(c.from_user.id, lid)
        if hw != "approved":
            await c.answer("Сначала отправьте домашку и дождитесь одобрения",
                           show_alert=True)
            return
    await mark_lesson_completed(c.from_user.id, lid)
    await c.answer("✅ Урок отмечен пройденным")

    # ищем следующий урок в этом курсе (по position, затем по id)
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        nxt = await (await conn.execute(
            "SELECT id, title FROM lessons WHERE course_id = ? "
            "AND (position > ? OR (position = ? AND id > ?)) "
            "ORDER BY position ASC, id ASC LIMIT 1",
            (lesson["course_id"], lesson["position"], lesson["position"], lid),
        )).fetchone()

    done, total = await course_progress(c.from_user.id, lesson["course_id"])
    if total and done >= total:
        await _send_certificate(c.from_user.id, lesson["course_id"])
        with suppress(TelegramAPIError):
            await bot.send_message(
                c.from_user.id,
                f"🎉 Курс пройден полностью ({done}/{total})!",
                reply_markup=kb_back(f"course:{lesson['course_id']}"),
            )
        return

    b = InlineKeyboardBuilder()
    if nxt:
        b.button(
            text=f"▶️ Следующий: {nxt['title'][:30]}",
            callback_data=f"lesson:{nxt['id']}",
        )
    b.button(text="⬅️ К курсу", callback_data=f"course:{lesson['course_id']}")
    b.adjust(1)
    with suppress(TelegramAPIError):
        await bot.send_message(
            c.from_user.id,
            f"Прогресс: <b>{done}/{total}</b> уроков пройдено.",
            reply_markup=b.as_markup(),
        )


async def _send_certificate(user_id: int, course_id: int) -> None:
    if not PIL_AVAILABLE:
        return
    course = await _get_course(course_id)
    if not course:
        return
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cached = await (await conn.execute(
            "SELECT file_id FROM certificates WHERE user_id = ? AND course_id = ?",
            (user_id, course_id),
        )).fetchone()
        user_row = await (await conn.execute(
            "SELECT username, full_name FROM users WHERE tg_id = ?", (user_id,)
        )).fetchone()
    if cached and cached["file_id"]:
        with suppress(TelegramAPIError):
            await bot.send_document(
                user_id, cached["file_id"],
                caption=f"🏆 Ваш сертификат по курсу «{esc(course['title'])}»",
            )
            return
    school = await setting_get("school_name", "Школа маникюра")
    name = (user_row["full_name"] if user_row and user_row["full_name"]
            else f"Ученик #{user_id}")
    pdf_bytes = _make_certificate_pdf(name, course["title"], school)
    if not pdf_bytes:
        return
    filename = f"certificate_{course_id}_{user_id}.pdf"
    try:
        msg = await bot.send_document(
            user_id,
            BufferedInputFile(pdf_bytes, filename=filename),
            caption=(
                f"🏆 Поздравляем! Вы завершили курс "
                f"<b>«{esc(course['title'])}»</b> и получаете сертификат."
            ),
        )
        file_id = msg.document.file_id if msg.document else None
        if file_id:
            async with aiosqlite.connect(DB_PATH) as conn:
                await conn.execute(
                    "INSERT OR REPLACE INTO certificates(user_id, course_id, file_id) "
                    "VALUES (?, ?, ?)",
                    (user_id, course_id, file_id),
                )
                await conn.commit()
    except TelegramAPIError as e:
        log.warning("send certificate failed: %s", e)


@router.callback_query(F.data.startswith("cert:"))
async def cb_cert(c: CallbackQuery) -> None:
    cid = int(c.data.split(":")[1])
    if not await has_course(c.from_user.id, cid):
        await c.answer("Курс не куплен", show_alert=True)
        return
    done, total = await course_progress(c.from_user.id, cid)
    if not total or done < total:
        await c.answer("Сертификат выдаётся после прохождения всех уроков",
                       show_alert=True)
        return
    await _send_certificate(c.from_user.id, cid)
    await c.answer()


@router.callback_query(F.data.startswith("chat:"))
async def cb_chat_link(c: CallbackQuery) -> None:
    cid = int(c.data.split(":")[1])
    if not await has_course(c.from_user.id, cid):
        await c.answer("Курс не куплен", show_alert=True)
        return
    course = await _get_course(cid)
    if not course or not course["chat_id"]:
        await c.answer("Чат не настроен", show_alert=True)
        return
    try:
        link = await bot.create_chat_invite_link(
            chat_id=int(course["chat_id"]),
            member_limit=1,
            expire_date=dt.datetime.now() + dt.timedelta(hours=24),
            name=f"course-{cid}-uid-{c.from_user.id}",
        )
    except TelegramAPIError as e:
        await c.answer(f"Ошибка: {e}", show_alert=True)
        return
    await bot.send_message(
        c.from_user.id,
        f"💬 Персональная ссылка на чат курса <b>{esc(course['title'])}</b> "
        f"(24 часа, одноразовая):\n\n{link.invite_link}",
    )
    await c.answer("Ссылка отправлена")


# --------------------------------------------------------------------------- #
# Отзывы
# --------------------------------------------------------------------------- #

@router.callback_query(F.data.startswith("review:"))
async def cb_review_start(c: CallbackQuery, state: FSMContext) -> None:
    cid = int(c.data.split(":")[1])
    if not await has_course(c.from_user.id, cid):
        await c.answer("Курс не куплен", show_alert=True)
        return
    await state.clear()
    await state.update_data(review_course_id=cid)
    await state.set_state(ReviewSG.stars)
    b = InlineKeyboardBuilder()
    for i in range(1, 6):
        b.button(text="⭐" * i, callback_data=f"review:star:{i}")
    b.button(text="⬅️ Отмена", callback_data=f"course:{cid}")
    b.adjust(5, 1)
    await bot.send_message(
        c.message.chat.id,
        "Оцените курс от 1 до 5 звёзд:",
        reply_markup=b.as_markup(),
    )
    await c.answer()


@router.callback_query(F.data.startswith("review:star:"), ReviewSG.stars)
async def cb_review_star(c: CallbackQuery, state: FSMContext) -> None:
    stars = int(c.data.split(":")[2])
    if stars < 1 or stars > 5:
        await c.answer()
        return
    await state.update_data(review_stars=stars)
    await state.set_state(ReviewSG.text)
    await c.message.edit_text(
        f"Оценка: {'⭐' * stars}\n\nНапишите текст отзыва "
        f"(или отправьте «-», чтобы оставить без текста):"
    )
    await c.answer()


@router.message(ReviewSG.text)
async def review_text(m: Message, state: FSMContext) -> None:
    data = await state.get_data()
    cid = int(data.get("review_course_id", 0))
    stars = int(data.get("review_stars", 5))
    if not cid:
        await state.clear()
        return
    text = (m.text or "").strip()
    if text == "-":
        text = ""
    text = text[:1000]
    await state.clear()
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        course = await (await conn.execute(
            "SELECT title FROM courses WHERE id = ?", (cid,)
        )).fetchone()
        await conn.execute(
            "INSERT INTO reviews(user_id, course_id, stars, text, status) "
            "VALUES (?, ?, ?, ?, 'pending') "
            "ON CONFLICT(user_id, course_id) DO UPDATE SET "
            "stars = excluded.stars, text = excluded.text, "
            "status = 'pending', created_at = datetime('now')",
            (m.from_user.id, cid, stars, text),
        )
        await conn.commit()
        rid_row = await (await conn.execute(
            "SELECT id FROM reviews WHERE user_id = ? AND course_id = ?",
            (m.from_user.id, cid),
        )).fetchone()
        rid = int(rid_row["id"])
    # в канал модерации
    caption = (
        f"⭐ <b>Новый отзыв #{rid}</b>\n"
        f"Курс: <b>{esc(course['title'] if course else '?')}</b>\n"
        f"Автор: <a href=\"tg://user?id={m.from_user.id}\">"
        f"{esc(m.from_user.full_name)}</a>\n"
        f"Оценка: {'⭐' * stars}\n\n"
        f"<blockquote>{esc(text) if text else '(без текста)'}</blockquote>"
    )
    bmod = InlineKeyboardBuilder()
    bmod.button(text="✅ Опубликовать", callback_data=f"mod:rev:approve:{rid}")
    bmod.button(text="❌ Отклонить", callback_data=f"mod:rev:reject:{rid}")
    bmod.adjust(1)
    try:
        mod_msg = await bot.send_message(
            LOG_CHANNEL_ID, caption, reply_markup=bmod.as_markup()
        )
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                "UPDATE reviews SET moderation_msg_id = ? WHERE id = ?",
                (mod_msg.message_id, rid),
            )
            await conn.commit()
    except TelegramAPIError as e:
        log.warning("review moderation notify: %s", e)
    await m.answer(
        "Спасибо! Отзыв отправлен на модерацию.",
        reply_markup=kb_main(is_admin(m.from_user.id)),
    )


@router.callback_query(F.data.startswith("mod:rev:"))
async def cb_mod_review(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        await c.answer("Нет прав", show_alert=True)
        return
    _, _, action, rid_raw = c.data.split(":")
    rid = int(rid_raw)
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        rev = await (await conn.execute(
            "SELECT * FROM reviews WHERE id = ?", (rid,)
        )).fetchone()
    if not rev:
        await c.answer("Не найдено", show_alert=True)
        return
    new_status = "approved" if action == "approve" else "rejected"
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE reviews SET status = ? WHERE id = ?", (new_status, rid)
        )
        await conn.commit()
    suffix = "\n\n✅ Опубликовано" if new_status == "approved" else "\n\n❌ Отклонено"
    with suppress(TelegramAPIError):
        await c.message.edit_text(
            (c.message.html_text or "") + suffix, reply_markup=None
        )
    with suppress(TelegramAPIError):
        if new_status == "approved":
            await bot.send_message(
                int(rev["user_id"]),
                f"🎉 Ваш отзыв к курсу опубликован."
            )
        else:
            await bot.send_message(
                int(rev["user_id"]),
                f"Ваш отзыв отклонён модератором."
            )
    await c.answer()


# --------------------------------------------------------------------------- #
# Домашние задания
# --------------------------------------------------------------------------- #

@router.callback_query(F.data.startswith("hw:"))
async def cb_hw_start(c: CallbackQuery, state: FSMContext) -> None:
    lid = int(c.data.split(":")[1])
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        lesson = await (await conn.execute(
            "SELECT * FROM lessons WHERE id = ?", (lid,)
        )).fetchone()
    if not lesson or not lesson["has_homework"]:
        await c.answer("Домашка не требуется", show_alert=True)
        return
    if not await has_course(c.from_user.id, lesson["course_id"]):
        await c.answer("Курс не куплен", show_alert=True)
        return
    # показать историю
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (await conn.execute(
            "SELECT id, status, reviewer_note, created_at FROM homeworks "
            "WHERE user_id = ? AND lesson_id = ? ORDER BY id DESC LIMIT 5",
            (c.from_user.id, lid),
        )).fetchall()
    hist = ""
    if rows:
        hist = "\n\n<b>История сдачи:</b>\n" + "\n".join(
            f"• #{r['id']} — "
            + {"pending": "⏳ на проверке",
               "approved": "✅ принята",
               "revise": "🔄 доработать"}.get(r["status"], r["status"])
            + (f" — <i>{esc(r['reviewer_note'])}</i>" if r["reviewer_note"] else "")
            for r in rows
        )
    last = rows[0] if rows else None
    await state.clear()
    await state.update_data(hw_lesson_id=lid)
    if last and last["status"] == "pending":
        await bot.send_message(
            c.message.chat.id,
            "⏳ Ваша домашка уже на проверке. Дождитесь решения." + hist,
        )
        await c.answer()
        return
    await state.set_state(HomeworkSG.content)
    await bot.send_message(
        c.message.chat.id,
        "📝 Пришлите одним сообщением: фото, видео, документ или текст "
        "с вашей работой. Можно с подписью." + hist,
        reply_markup=kb_back(f"lesson:{lid}"),
    )
    await c.answer()


@router.message(HomeworkSG.content)
async def hw_submit(m: Message, state: FSMContext) -> None:
    data = await state.get_data()
    lid = int(data.get("hw_lesson_id", 0))
    if not lid:
        await state.clear()
        return
    await state.clear()
    ct, fid, tx = _extract_content(m)
    if ct is None:
        await m.answer("Поддерживаются: фото, видео, документ, аудио, голос, GIF, кружок, текст.")
        return
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        lesson = await (await conn.execute(
            "SELECT l.*, c.title AS course_title FROM lessons l "
            "JOIN courses c ON c.id = l.course_id WHERE l.id = ?",
            (lid,),
        )).fetchone()
        cur = await conn.execute(
            "INSERT INTO homeworks(user_id, lesson_id, content_type, content_file_id, "
            "content_text, caption, status) VALUES (?, ?, ?, ?, ?, ?, 'pending')",
            (m.from_user.id, lid, ct, fid, tx if ct == "text" else None,
             m.caption or ""),
        )
        hid = cur.lastrowid
        await conn.commit()
    # форвардим в канал модерации
    header = (
        f"📝 <b>Домашка #{hid}</b>\n"
        f"Курс: <b>{esc(lesson['course_title'])}</b>\n"
        f"Урок: <b>{esc(lesson['title'])}</b>\n"
        f"Ученик: <a href=\"tg://user?id={m.from_user.id}\">"
        f"{esc(m.from_user.full_name)}</a> (<code>{m.from_user.id}</code>)"
    )
    bmod = InlineKeyboardBuilder()
    bmod.button(text="✅ Принять", callback_data=f"mod:hw:approve:{hid}")
    bmod.button(text="🔄 Доработать", callback_data=f"mod:hw:revise:{hid}")
    bmod.adjust(1)
    try:
        fwd = await m.forward(LOG_CHANNEL_ID)
        msg = await bot.send_message(
            LOG_CHANNEL_ID, header,
            reply_markup=bmod.as_markup(),
            reply_parameters=ReplyParameters(message_id=fwd.message_id),
        )
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                "UPDATE homeworks SET moderation_msg_id = ? WHERE id = ?",
                (msg.message_id, hid),
            )
            await conn.commit()
    except TelegramAPIError as e:
        log.warning("hw moderation notify: %s", e)

    await m.answer(
        "📨 Домашка отправлена на проверку. Ждите решения преподавателя.",
        reply_markup=kb_main(is_admin(m.from_user.id)),
    )


@router.callback_query(F.data.startswith("mod:hw:"))
async def cb_mod_hw(c: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(c.from_user.id):
        await c.answer("Нет прав", show_alert=True)
        return
    _, _, action, hid_raw = c.data.split(":")
    hid = int(hid_raw)
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        hw = await (await conn.execute(
            "SELECT * FROM homeworks WHERE id = ?", (hid,)
        )).fetchone()
    if not hw or hw["status"] != "pending":
        await c.answer("Уже обработано", show_alert=True)
        return
    if action == "approve":
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                "UPDATE homeworks SET status = 'approved', "
                "reviewed_at = datetime('now') WHERE id = ?", (hid,)
            )
            await conn.commit()
        suffix = "\n\n✅ Домашка принята"
        with suppress(TelegramAPIError):
            await bot.send_message(
                int(hw["user_id"]),
                "✅ Ваша домашка принята! Можете отметить урок пройденным.",
            )
        with suppress(TelegramAPIError):
            await c.message.edit_text(
                (c.message.html_text or "") + suffix, reply_markup=None
            )
        await c.answer()
    else:
        await state.clear()
        await state.update_data(hw_moderate_id=hid, hw_moderate_msg_id=c.message.message_id)
        await state.set_state(AdminSG.homework_note)
        await c.message.reply(
            "✍️ Напишите комментарий ученику (что нужно исправить):"
        )
        await c.answer()


@router.message(AdminSG.homework_note)
async def hw_revise_note(m: Message, state: FSMContext) -> None:
    if not is_admin(m.from_user.id):
        return
    data = await state.get_data()
    hid = int(data.get("hw_moderate_id", 0))
    await state.clear()
    note = (m.text or "").strip()[:500]
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        hw = await (await conn.execute(
            "SELECT * FROM homeworks WHERE id = ?", (hid,)
        )).fetchone()
        if not hw:
            return
        await conn.execute(
            "UPDATE homeworks SET status = 'revise', reviewer_note = ?, "
            "reviewed_at = datetime('now') WHERE id = ?", (note, hid)
        )
        await conn.commit()
    with suppress(TelegramAPIError):
        await bot.send_message(
            int(hw["user_id"]),
            f"🔄 Домашку нужно переделать.\n\n<b>Комментарий преподавателя:</b>\n"
            f"<blockquote>{esc(note)}</blockquote>",
        )
    mod_msg_id = int(data.get("hw_moderate_msg_id", 0))
    if mod_msg_id:
        with suppress(TelegramAPIError):
            await bot.edit_message_text(
                f"(обработано, отправлена доработка)",
                chat_id=LOG_CHANNEL_ID, message_id=mod_msg_id, reply_markup=None,
            )
    await m.answer("📨 Комментарий отправлен ученику.")


def _extract_content(m: Message) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Вернёт (content_type, file_id, text) на основе типа сообщения."""
    if m.photo:
        return "photo", m.photo[-1].file_id, None
    if m.video:
        return "video", m.video.file_id, None
    if m.document:
        return "document", m.document.file_id, None
    if m.audio:
        return "audio", m.audio.file_id, None
    if m.voice:
        return "voice", m.voice.file_id, None
    if m.animation:
        return "animation", m.animation.file_id, None
    if m.video_note:
        return "video_note", m.video_note.file_id, None
    if m.text:
        return "text", None, m.text
    return None, None, None


# --------------------------------------------------------------------------- #
# Оплата Telegram Stars
# --------------------------------------------------------------------------- #

@router.callback_query(F.data.startswith("buy:stars:"))
async def cb_buy_stars(c: CallbackQuery, state: FSMContext) -> None:
    if not STARS_ENABLED:
        await c.answer("Stars отключены", show_alert=True)
        return
    cid = int(c.data.split(":")[2])
    ctx = await _get_buy_context(c.from_user.id, cid, state)
    if not ctx:
        await c.answer("Курс не найден", show_alert=True)
        return
    course, price, promo = ctx
    stars = course["star_price"] or rub_to_stars(price)
    # создаём deposit
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "INSERT INTO deposits(user_id, course_id, method, amount, promo_code, status) "
            "VALUES (?, ?, 'stars', ?, ?, 'pending')",
            (c.from_user.id, cid, price, promo),
        )
        did = cur.lastrowid
        await conn.commit()
    try:
        await bot.send_invoice(
            chat_id=c.from_user.id,
            title=f"Курс «{course['title'][:40]}»",
            description=(course["short_description"] or course["title"])[:255],
            payload=f"stars:{did}",
            currency="XTR",
            prices=[LabeledPrice(label=f"Курс", amount=int(stars))],
        )
    except TelegramAPIError as e:
        await c.answer(f"Ошибка Stars: {e}", show_alert=True)
        return
    await c.answer()


@router.pre_checkout_query()
async def on_precheckout(q: PreCheckoutQuery) -> None:
    await q.answer(ok=True)


@router.message(F.successful_payment)
async def on_successful_payment(m: Message) -> None:
    sp = m.successful_payment
    if not sp:
        return
    payload = sp.invoice_payload or ""
    if payload.startswith("stars:"):
        try:
            did = int(payload.split(":", 1)[1])
        except ValueError:
            return
        await _finalize_purchase(did)
        await m.answer(
            "✅ Оплата Stars получена! Курс открыт в разделе «Мои курсы».",
            reply_markup=kb_main(is_admin(m.from_user.id)),
        )


# --------------------------------------------------------------------------- #
# Оплата YooKassa
# --------------------------------------------------------------------------- #

@router.callback_query(F.data.startswith("buy:yk:check:"))
async def cb_buy_yk_check(c: CallbackQuery) -> None:
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
        await c.answer("Уже оплачено", show_alert=True)
        return
    if not dep["method_extra"]:
        await c.answer("Нет payment_id", show_alert=True)
        return
    try:
        data = await yookassa.get_payment(dep["method_extra"])
    except Exception as e:
        log.exception("yookassa get_payment: %s", e)
        await c.answer("Ошибка запроса", show_alert=True)
        return
    if data.get("status") != "succeeded":
        await c.answer(
            f"Оплата ещё не поступила (статус: {data.get('status', '?')})",
            show_alert=True,
        )
        return
    await _finalize_purchase(did)
    with suppress(TelegramAPIError):
        await c.message.edit_text(
            "✅ Оплата получена! Курс открыт.",
            reply_markup=kb_back("main"),
        )
    await c.answer("Оплачено!")


@router.callback_query(F.data.startswith("buy:yk:"))
async def cb_buy_yk(c: CallbackQuery, state: FSMContext) -> None:
    if not yookassa.enabled:
        await c.answer("YooKassa не настроена", show_alert=True)
        return
    cid = int(c.data.split(":")[2])
    ctx = await _get_buy_context(c.from_user.id, cid, state)
    if not ctx:
        await c.answer("Курс не найден", show_alert=True)
        return
    course, price, promo = ctx
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "INSERT INTO deposits(user_id, course_id, method, amount, promo_code, status) "
            "VALUES (?, ?, 'yookassa', ?, ?, 'pending')",
            (c.from_user.id, cid, price, promo),
        )
        did = cur.lastrowid
        await conn.commit()
    try:
        me = await bot.me()
        pay = await yookassa.create_payment(
            amount_rub=price / 100,
            description=f"Курс «{course['title'][:80]}»",
            return_url=f"https://t.me/{me.username}",
        )
    except Exception as e:
        log.exception("yookassa create: %s", e)
        await c.answer("Не удалось создать платёж", show_alert=True)
        return
    pay_url = (pay.get("confirmation") or {}).get("confirmation_url", "")
    payment_id = pay.get("id")
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE deposits SET method_extra = ? WHERE id = ?", (payment_id, did)
        )
        await conn.commit()
    b = InlineKeyboardBuilder()
    b.button(text=f"🏦 Оплатить {rub(price)}", url=pay_url)
    b.button(text="🔄 Я оплатил(а)", callback_data=f"buy:yk:check:{did}")
    b.button(text="⬅️ К курсу", callback_data=f"course:{cid}")
    b.adjust(1)
    await bot.send_message(
        c.message.chat.id,
        f"🏦 Оплата через YooKassa: <b>{esc(rub(price))}</b>\n\n"
        f"Нажмите «Оплатить», после оплаты — «Я оплатил(а)».",
        reply_markup=b.as_markup(),
    )
    await c.answer()


# --------------------------------------------------------------------------- #
# Админ — категории
# --------------------------------------------------------------------------- #

@router.callback_query(F.data == "admin:cats")
async def cb_admin_cats(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (await conn.execute(
            "SELECT c.id, c.name, COUNT(co.id) AS cnt FROM categories c "
            "LEFT JOIN courses co ON co.category_id = c.id "
            "GROUP BY c.id ORDER BY c.position, c.id"
        )).fetchall()
    b = InlineKeyboardBuilder()
    for r in rows:
        b.button(text=f"🗂 {r['name']} ({r['cnt']})",
                 callback_data=f"admin:cat_edit:{r['id']}")
    b.button(text="➕ Новая категория", callback_data="admin:cat_new")
    b.button(text="⬅️ В админ-панель", callback_data="admin:menu")
    b.adjust(1)
    await c.message.edit_text(
        f"<b>🗂 Категории</b>\nВсего: <b>{len(rows)}</b>",
        reply_markup=b.as_markup(),
    )
    await c.answer()


@router.callback_query(F.data == "admin:cat_new")
async def cb_admin_cat_new(c: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(c.from_user.id):
        return
    await state.clear()
    await state.set_state(AdminSG.category_name)
    await c.message.edit_text("Введите название новой категории:",
                              reply_markup=kb_back("admin:cats"))
    await c.answer()


@router.message(AdminSG.category_name)
async def admin_cat_create(m: Message, state: FSMContext) -> None:
    if not is_admin(m.from_user.id):
        return
    name = (m.text or "").strip()[:64]
    await state.clear()
    if not name:
        await m.answer("Имя не может быть пустым")
        return
    try:
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                "INSERT INTO categories(name, position) VALUES (?, "
                "(SELECT COALESCE(MAX(position), 0) + 1 FROM categories))",
                (name,),
            )
            await conn.commit()
    except aiosqlite.IntegrityError:
        await m.answer("Категория с таким именем уже есть")
        return
    await m.answer(f"✅ Категория <b>{esc(name)}</b> создана.",
                   reply_markup=kb_back("admin:cats"))


@router.callback_query(F.data.startswith("admin:cat_edit:"))
async def cb_admin_cat_edit(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    cat_id = int(c.data.split(":")[2])
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cat = await (await conn.execute(
            "SELECT * FROM categories WHERE id = ?", (cat_id,)
        )).fetchone()
        cnt = (await (await conn.execute(
            "SELECT COUNT(*) AS c FROM courses WHERE category_id = ?", (cat_id,)
        )).fetchone())["c"]
    if not cat:
        await c.answer("Не найдено", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    b.button(text="✏️ Переименовать", callback_data=f"admin:cat_rename:{cat_id}")
    b.button(text="🗑 Удалить", callback_data=f"admin:cat_del:{cat_id}")
    b.button(text="⬅️ К категориям", callback_data="admin:cats")
    b.adjust(1)
    await c.message.edit_text(
        f"<b>🗂 {esc(cat['name'])}</b>\nКурсов в категории: <b>{cnt}</b>",
        reply_markup=b.as_markup(),
    )
    await c.answer()


@router.callback_query(F.data.startswith("admin:cat_rename:"))
async def cb_admin_cat_rename(c: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(c.from_user.id):
        return
    cat_id = int(c.data.split(":")[2])
    await state.clear()
    await state.update_data(rename_cat_id=cat_id)
    await state.set_state(AdminSG.category_rename)
    await c.message.edit_text("Введите новое название:",
                              reply_markup=kb_back(f"admin:cat_edit:{cat_id}"))
    await c.answer()


@router.message(AdminSG.category_rename)
async def admin_cat_rename_apply(m: Message, state: FSMContext) -> None:
    if not is_admin(m.from_user.id):
        return
    data = await state.get_data()
    cat_id = int(data.get("rename_cat_id", 0))
    new_name = (m.text or "").strip()[:64]
    await state.clear()
    if not cat_id or not new_name:
        return
    try:
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                "UPDATE categories SET name = ? WHERE id = ?", (new_name, cat_id)
            )
            await conn.commit()
    except aiosqlite.IntegrityError:
        await m.answer("Категория с таким именем уже есть")
        return
    await m.answer(f"✅ Переименовано в <b>{esc(new_name)}</b>.",
                   reply_markup=kb_back(f"admin:cat_edit:{cat_id}"))


@router.callback_query(F.data.startswith("admin:cat_del:"))
async def cb_admin_cat_del(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    cat_id = int(c.data.split(":")[2])
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE courses SET category_id = NULL WHERE category_id = ?", (cat_id,)
        )
        await conn.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
        await conn.commit()
    await c.answer("Удалено")
    await cb_admin_cats(c)


# --------------------------------------------------------------------------- #
# Админ — привязка курса к категории, chat_id, star_price, домашки
# --------------------------------------------------------------------------- #

@router.callback_query(F.data.startswith("admin:course_cat:"))
async def cb_admin_course_cat(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    cid = int(c.data.split(":")[2])
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cats = await (await conn.execute(
            "SELECT id, name FROM categories ORDER BY position, id"
        )).fetchall()
    b = InlineKeyboardBuilder()
    b.button(text="(без категории)", callback_data=f"admin:course_setcat:{cid}:0")
    for cat in cats:
        b.button(text=cat["name"], callback_data=f"admin:course_setcat:{cid}:{cat['id']}")
    b.button(text="⬅️ К курсу", callback_data=f"admin:course:{cid}")
    b.adjust(1)
    await c.message.edit_text("Выберите категорию для курса:",
                              reply_markup=b.as_markup())
    await c.answer()


@router.callback_query(F.data.startswith("admin:course_setcat:"))
async def cb_admin_course_setcat(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    _, _, cid_raw, cat_raw = c.data.split(":")
    cid = int(cid_raw)
    cat_id = int(cat_raw) or None
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE courses SET category_id = ? WHERE id = ?", (cat_id, cid)
        )
        await conn.commit()
    await c.answer("Сохранено")
    # вернёмся к карточке курса (админ)
    c.data = f"admin:course:{cid}"
    await cb_admin_course(c) if "cb_admin_course" in globals() else None


@router.callback_query(F.data.startswith("admin:course_chat:"))
async def cb_admin_course_chat(c: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(c.from_user.id):
        return
    cid = int(c.data.split(":")[2])
    await state.clear()
    await state.update_data(chat_course_id=cid)
    await state.set_state(AdminSG.course_chat_id)
    await c.message.edit_text(
        "Введите chat_id закрытого чата курса (формат <code>-100...</code>). "
        "Бот должен быть в нём админом с правом приглашать.\n"
        "Или отправьте «0» чтобы отвязать.",
        reply_markup=kb_back(f"admin:course:{cid}"),
    )
    await c.answer()


@router.message(AdminSG.course_chat_id)
async def admin_course_chat_set(m: Message, state: FSMContext) -> None:
    if not is_admin(m.from_user.id):
        return
    data = await state.get_data()
    cid = int(data.get("chat_course_id", 0))
    await state.clear()
    if not cid:
        return
    raw = (m.text or "").strip()
    try:
        chat_id_val: Optional[int] = int(raw)
    except ValueError:
        await m.answer("Неверный формат. Пришлите число.")
        return
    if chat_id_val == 0:
        chat_id_val = None
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE courses SET chat_id = ? WHERE id = ?", (chat_id_val, cid)
        )
        await conn.commit()
    if chat_id_val:
        try:
            chat = await bot.get_chat(chat_id_val)
            await m.answer(
                f"✅ Привязан чат: <b>{esc(chat.title or str(chat_id_val))}</b>"
            )
        except TelegramAPIError as e:
            await m.answer(
                f"⚠️ Сохранил chat_id={chat_id_val}, но доступа к чату пока нет: {esc(str(e))}"
            )
    else:
        await m.answer("✅ Чат отвязан от курса.")


@router.callback_query(F.data.startswith("admin:course_star:"))
async def cb_admin_course_star(c: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(c.from_user.id):
        return
    cid = int(c.data.split(":")[2])
    await state.clear()
    await state.update_data(star_course_id=cid)
    await state.set_state(AdminSG.course_star_price)
    await c.message.edit_text(
        "Введите цену курса в Telegram Stars (целое число ≥ 1). "
        "Или «0» чтобы считать автоматически из цены в ₽.",
        reply_markup=kb_back(f"admin:course:{cid}"),
    )
    await c.answer()


@router.message(AdminSG.course_star_price)
async def admin_course_star_set(m: Message, state: FSMContext) -> None:
    if not is_admin(m.from_user.id):
        return
    data = await state.get_data()
    cid = int(data.get("star_course_id", 0))
    await state.clear()
    if not cid:
        return
    try:
        val = int((m.text or "").strip())
    except ValueError:
        await m.answer("Нужно число")
        return
    star_price = val if val > 0 else None
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE courses SET star_price = ? WHERE id = ?", (star_price, cid)
        )
        await conn.commit()
    await m.answer(
        f"✅ Цена в Stars: <b>{star_price if star_price else 'автоматически'}</b>"
    )


@router.callback_query(F.data.startswith("admin:lesson_toghw:"))
async def cb_admin_lesson_toghw(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    lid = int(c.data.split(":")[2])
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE lessons SET has_homework = 1 - COALESCE(has_homework, 0) WHERE id = ?",
            (lid,),
        )
        await conn.commit()
    await c.answer("Переключено")
    c.data = f"admin:lesson:{lid}"
    with suppress(Exception):
        await globals()["cb_admin_lesson"](c)


# --------------------------------------------------------------------------- #
# Админ — отзывы
# --------------------------------------------------------------------------- #

@router.callback_query(F.data.startswith("admin:reviews:"))
async def cb_admin_reviews(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    page = int(c.data.split(":")[2])
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (await conn.execute(
            "SELECT r.*, c.title AS course_title, u.full_name AS author "
            "FROM reviews r JOIN courses c ON c.id = r.course_id "
            "LEFT JOIN users u ON u.tg_id = r.user_id "
            "ORDER BY r.id DESC LIMIT ? OFFSET ?",
            (PAGE_SIZE, page * PAGE_SIZE),
        )).fetchall()
        total = (await (await conn.execute(
            "SELECT COUNT(*) AS c FROM reviews"
        )).fetchone())["c"]
    if not rows:
        await c.message.edit_text("Отзывов пока нет.", reply_markup=kb_back("admin:menu"))
        await c.answer()
        return
    lines = [f"<b>⭐ Отзывы</b> (всего {total})\n"]
    for r in rows:
        tag = {"approved": "✅", "rejected": "❌",
               "pending": "⏳"}.get(r["status"], "?")
        lines.append(
            f"#{r['id']} {tag} {'⭐' * r['stars']} — "
            f"<b>{esc(r['course_title'][:30])}</b> от "
            f"{esc(r['author'] or str(r['user_id']))}\n"
            f"<i>{esc((r['text'] or '')[:100])}</i>"
        )
    b = InlineKeyboardBuilder()
    for r in rows:
        if r["status"] == "pending":
            b.button(text=f"#{r['id']} ✅", callback_data=f"mod:rev:approve:{r['id']}")
            b.button(text=f"#{r['id']} ❌", callback_data=f"mod:rev:reject:{r['id']}")
    b.adjust(2)
    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="⬅️", callback_data=f"admin:reviews:{page-1}")
    if (page + 1) * PAGE_SIZE < total:
        nav.button(text="➡️", callback_data=f"admin:reviews:{page+1}")
    nav.button(text="⬅️ В админ-панель", callback_data="admin:menu")
    nav.adjust(2, 1)
    b.attach(nav)
    await c.message.edit_text("\n\n".join(lines), reply_markup=b.as_markup())
    await c.answer()


# --------------------------------------------------------------------------- #
# Админ — домашки (общий список)
# --------------------------------------------------------------------------- #

@router.callback_query(F.data.startswith("admin:hw:"))
async def cb_admin_hw(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    page = int(c.data.split(":")[2])
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (await conn.execute(
            "SELECT h.id, h.status, h.created_at, l.title AS l_title, "
            "c.title AS c_title, u.full_name AS author, h.user_id "
            "FROM homeworks h "
            "JOIN lessons l ON l.id = h.lesson_id "
            "JOIN courses c ON c.id = l.course_id "
            "LEFT JOIN users u ON u.tg_id = h.user_id "
            "WHERE h.status = 'pending' "
            "ORDER BY h.id DESC LIMIT ? OFFSET ?",
            (PAGE_SIZE, page * PAGE_SIZE),
        )).fetchall()
        total = (await (await conn.execute(
            "SELECT COUNT(*) AS c FROM homeworks WHERE status = 'pending'"
        )).fetchone())["c"]
    if not rows and page == 0:
        await c.message.edit_text(
            "Нет домашек на проверке.", reply_markup=kb_back("admin:menu")
        )
        await c.answer()
        return
    lines = [f"<b>📝 Домашки на проверке</b> ({total})"]
    for r in rows:
        lines.append(
            f"#{r['id']} — <b>{esc(r['c_title'][:30])}</b> / "
            f"<i>{esc(r['l_title'][:30])}</i>\n"
            f"от {esc(r['author'] or str(r['user_id']))} · "
            f"<code>{esc(r['created_at'])}</code>"
        )
    b = InlineKeyboardBuilder()
    for r in rows:
        b.button(text=f"#{r['id']} ✅", callback_data=f"mod:hw:approve:{r['id']}")
        b.button(text=f"#{r['id']} 🔄", callback_data=f"mod:hw:revise:{r['id']}")
    b.adjust(2)
    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="⬅️", callback_data=f"admin:hw:{page-1}")
    if (page + 1) * PAGE_SIZE < total:
        nav.button(text="➡️", callback_data=f"admin:hw:{page+1}")
    nav.button(text="⬅️ В админ-панель", callback_data="admin:menu")
    nav.adjust(2, 1)
    b.attach(nav)
    await c.message.edit_text("\n\n".join(lines), reply_markup=b.as_markup())
    await c.answer()


# --------------------------------------------------------------------------- #
# Админ — название школы
# --------------------------------------------------------------------------- #

@router.callback_query(F.data == "admin:school")
async def cb_admin_school(c: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(c.from_user.id):
        return
    current = await setting_get("school_name", "Школа маникюра")
    await state.clear()
    await state.set_state(AdminSG.set_school_name)
    await c.message.edit_text(
        f"<b>🏫 Название школы</b>\n\nТекущее: <b>{esc(current)}</b>\n\n"
        f"Пришлите новое название (используется на сертификатах).",
        reply_markup=kb_back("admin:menu"),
    )
    await c.answer()


@router.message(AdminSG.set_school_name)
async def admin_school_set(m: Message, state: FSMContext) -> None:
    if not is_admin(m.from_user.id):
        return
    await state.clear()
    name = (m.text or "").strip()[:120]
    if not name:
        return
    await setting_set("school_name", name)
    await m.answer(f"✅ Название школы: <b>{esc(name)}</b>",
                   reply_markup=kb_back("admin:menu"))


# --------------------------------------------------------------------------- #
# 📰 Новости
# --------------------------------------------------------------------------- #

NEWS_PAGE = 5


def _news_media_kinds() -> set[str]:
    return {"photo", "video", "animation", "document"}


async def _send_news_post(chat_id: int, item: aiosqlite.Row,
                          reply_markup: Optional[InlineKeyboardMarkup] = None,
                          ) -> None:
    """Отправляет один пост (текст + опц. медиа) — пользователю или админу."""
    pin = "📌 " if item["is_pinned"] else ""
    header = f"<b>{pin}{esc(item['title'])}</b>"
    body = esc(item["body"]) if item["body"] else ""
    full_text = (header + ("\n\n" + body if body else "")).strip()
    media_type = item["media_type"]
    file_id = item["media_file_id"]
    if media_type and file_id:
        caption = full_text[:1024]
        try:
            if media_type == "photo":
                await bot.send_photo(chat_id, file_id, caption=caption,
                                     reply_markup=reply_markup)
            elif media_type == "video":
                await bot.send_video(chat_id, file_id, caption=caption,
                                     reply_markup=reply_markup)
            elif media_type == "animation":
                await bot.send_animation(chat_id, file_id, caption=caption,
                                         reply_markup=reply_markup)
            elif media_type == "document":
                await bot.send_document(chat_id, file_id, caption=caption,
                                        reply_markup=reply_markup)
            else:
                await bot.send_message(chat_id, full_text, reply_markup=reply_markup)
            return
        except TelegramAPIError:
            # fallback: текст + ссылка обратно
            pass
    await bot.send_message(chat_id, full_text or "—", reply_markup=reply_markup)


# --- Пользовательская лента --- #

@router.callback_query(F.data.startswith("news:"))
async def cb_news_user(c: CallbackQuery) -> None:
    parts = c.data.split(":")
    if len(parts) >= 3 and parts[1] == "view":
        await _user_news_view(c, int(parts[2]))
        return
    page = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        total = (await (await conn.execute(
            "SELECT COUNT(*) AS c FROM news WHERE is_published = 1"
        )).fetchone())["c"]
        rows = await (await conn.execute(
            "SELECT id, title, is_pinned, created_at FROM news "
            "WHERE is_published = 1 "
            "ORDER BY is_pinned DESC, id DESC "
            "LIMIT ? OFFSET ?",
            (NEWS_PAGE, page * NEWS_PAGE),
        )).fetchall()
    if total == 0:
        with suppress(TelegramAPIError):
            await c.message.edit_text(
                "<b>📰 Новости</b>\n\nПока тут пусто. Загляните позже.",
                reply_markup=kb_back("main"),
            )
        await c.answer()
        return
    lines = ["<b>📰 Новости</b>", ""]
    b = InlineKeyboardBuilder()
    for r in rows:
        prefix = "📌 " if r["is_pinned"] else ""
        date = (r["created_at"] or "")[:10]
        lines.append(f"• {prefix}<b>{esc(r['title'])}</b> — <i>{esc(date)}</i>")
        b.button(
            text=f"{prefix}{r['title'][:36]}",
            callback_data=f"news:view:{r['id']}",
        )
    b.adjust(1)
    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="⬅️", callback_data=f"news:{page-1}")
    if (page + 1) * NEWS_PAGE < total:
        nav.button(text="➡️", callback_data=f"news:{page+1}")
    nav.adjust(2)
    b.attach(nav)
    tail = InlineKeyboardBuilder()
    tail.button(text="⬅️ В меню", callback_data="main")
    tail.adjust(1)
    b.attach(tail)
    with suppress(TelegramAPIError):
        await c.message.edit_text("\n".join(lines), reply_markup=b.as_markup())
    await c.answer()


async def _user_news_view(c: CallbackQuery, news_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        item = await (await conn.execute(
            "SELECT * FROM news WHERE id = ? AND is_published = 1", (news_id,)
        )).fetchone()
    if not item:
        await c.answer("Новость не найдена или снята с публикации", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ К списку", callback_data="news:0")
    b.adjust(1)
    await _send_news_post(c.from_user.id, item, reply_markup=b.as_markup())
    await c.answer()


# --- Админ-CRUD --- #

@router.callback_query(F.data.startswith("admin:news:"))
async def cb_admin_news_list(c: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(c.from_user.id):
        return
    await state.clear()
    page = int(c.data.split(":")[2])
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        total = (await (await conn.execute(
            "SELECT COUNT(*) AS c FROM news"
        )).fetchone())["c"]
        rows = await (await conn.execute(
            "SELECT id, title, is_published, is_pinned, broadcast_done, created_at "
            "FROM news ORDER BY is_pinned DESC, id DESC LIMIT ? OFFSET ?",
            (NEWS_PAGE, page * NEWS_PAGE),
        )).fetchall()
    text = f"<b>📰 Новости (всего: {total})</b>"
    b = InlineKeyboardBuilder()
    if total == 0:
        text += "\n\nЕщё ничего не добавлено."
    for r in rows:
        flags = []
        if r["is_pinned"]:
            flags.append("📌")
        flags.append("🟢" if r["is_published"] else "⚪️")
        if r["broadcast_done"]:
            flags.append("📢")
        b.button(
            text=f"{''.join(flags)} #{r['id']} {r['title'][:30]}",
            callback_data=f"admin:news_view:{r['id']}",
        )
    b.adjust(1)
    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="⬅️", callback_data=f"admin:news:{page-1}")
    if (page + 1) * NEWS_PAGE < total:
        nav.button(text="➡️", callback_data=f"admin:news:{page+1}")
    nav.adjust(2)
    b.attach(nav)
    tail = InlineKeyboardBuilder()
    tail.button(text="➕ Новая новость", callback_data="admin:news_new")
    tail.button(text="⬅️ Админ", callback_data="admin:menu")
    tail.adjust(1)
    b.attach(tail)
    with suppress(TelegramAPIError):
        await c.message.edit_text(text, reply_markup=b.as_markup())
    await c.answer()


@router.callback_query(F.data == "admin:news_new")
async def cb_admin_news_new(c: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(c.from_user.id):
        return
    await state.set_state(NewsSG.title)
    await c.message.edit_text(
        "<b>📰 Новая новость — 1/3</b>\n\nВведите заголовок (до 120 символов):",
        reply_markup=kb_back("admin:news:0"),
    )
    await c.answer()


@router.message(NewsSG.title)
async def admin_news_title(m: Message, state: FSMContext) -> None:
    title = (m.text or "").strip()
    if not title:
        await m.answer("Заголовок не может быть пустым.")
        return
    await state.update_data(title=title[:120])
    await state.set_state(NewsSG.body)
    b = InlineKeyboardBuilder()
    b.button(text="⏭ Пропустить (без текста)", callback_data="admin:news_skip_body")
    b.button(text="⬅️ Отмена", callback_data="admin:news:0")
    b.adjust(1)
    await m.answer(
        "<b>2/3</b>\n\nПришлите текст новости (HTML поддерживается) "
        "или нажмите «Пропустить»:",
        reply_markup=b.as_markup(),
    )


@router.callback_query(NewsSG.body, F.data == "admin:news_skip_body")
async def cb_admin_news_skip_body(c: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(body="")
    await _ask_news_media(c.message, state)
    await c.answer()


@router.message(NewsSG.body)
async def admin_news_body(m: Message, state: FSMContext) -> None:
    body = (m.html_text or m.text or "").strip()
    await state.update_data(body=body[:3500])
    await _ask_news_media(m, state)


async def _ask_news_media(target: Message, state: FSMContext) -> None:
    await state.set_state(NewsSG.media)
    b = InlineKeyboardBuilder()
    b.button(text="⏭ Пропустить (без медиа)", callback_data="admin:news_skip_media")
    b.button(text="⬅️ Отмена", callback_data="admin:news:0")
    b.adjust(1)
    await target.answer(
        "<b>3/3</b>\n\nПришлите фото/видео/GIF/документ (необязательно), "
        "или «Пропустить»:",
        reply_markup=b.as_markup(),
    )


@router.callback_query(NewsSG.media, F.data == "admin:news_skip_media")
async def cb_admin_news_skip_media(c: CallbackQuery, state: FSMContext) -> None:
    await _save_news(c.from_user.id, state, media_type=None, file_id=None,
                     reply_target=c.message)
    await c.answer()


@router.message(NewsSG.media)
async def admin_news_media(m: Message, state: FSMContext) -> None:
    media_type, file_id = None, None
    if m.photo:
        media_type, file_id = "photo", m.photo[-1].file_id
    elif m.video:
        media_type, file_id = "video", m.video.file_id
    elif m.animation:
        media_type, file_id = "animation", m.animation.file_id
    elif m.document:
        media_type, file_id = "document", m.document.file_id
    else:
        await m.answer("Поддерживается фото / видео / GIF / документ. "
                       "Или нажмите «Пропустить» выше.")
        return
    await _save_news(m.from_user.id, state, media_type=media_type,
                     file_id=file_id, reply_target=m)


async def _save_news(user_id: int, state: FSMContext, *, media_type: Optional[str],
                     file_id: Optional[str], reply_target: Message) -> None:
    data = await state.get_data()
    await state.clear()
    title = data.get("title", "Без заголовка")
    body = data.get("body", "")
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "INSERT INTO news(title, body, media_type, media_file_id) "
            "VALUES (?, ?, ?, ?)",
            (title, body, media_type, file_id),
        )
        await conn.commit()
        nid = cur.lastrowid
    b = InlineKeyboardBuilder()
    b.button(text="🟢 Опубликовать", callback_data=f"admin:news_pub:{nid}")
    b.button(text="📰 К списку", callback_data="admin:news:0")
    b.adjust(1)
    await reply_target.answer(
        f"✅ Новость #{nid} создана как черновик. Опубликуйте, чтобы она "
        f"появилась у пользователей.",
        reply_markup=b.as_markup(),
    )


async def _admin_news_card(news_id: int) -> tuple[Optional[str], Optional[InlineKeyboardMarkup]]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        item = await (await conn.execute(
            "SELECT * FROM news WHERE id = ?", (news_id,)
        )).fetchone()
    if not item:
        return None, None
    pub = "🟢 опубликована" if item["is_published"] else "⚪️ черновик"
    pin = "📌 закреплена" if item["is_pinned"] else ""
    cast = "📢 разослана" if item["broadcast_done"] else ""
    media = f"{item['media_type']}" if item["media_type"] else "—"
    body_preview = (item["body"] or "")[:300]
    text = (
        f"<b>📰 #{item['id']} {esc(item['title'])}</b>\n"
        f"Статус: <b>{pub}</b> {pin} {cast}\n"
        f"Медиа: <b>{esc(media)}</b>\n"
        f"Создано: {esc((item['created_at'] or '')[:16])}\n\n"
        f"{esc(body_preview)}"
    )
    b = InlineKeyboardBuilder()
    if item["is_published"]:
        b.button(text="⚪️ Снять с публикации",
                 callback_data=f"admin:news_unpub:{item['id']}")
    else:
        b.button(text="🟢 Опубликовать",
                 callback_data=f"admin:news_pub:{item['id']}")
    if item["is_pinned"]:
        b.button(text="📌 Открепить",
                 callback_data=f"admin:news_unpin:{item['id']}")
    else:
        b.button(text="📌 Закрепить",
                 callback_data=f"admin:news_pin:{item['id']}")
    b.button(text="✏️ Заголовок", callback_data=f"admin:news_edit:{item['id']}:title")
    b.button(text="✏️ Текст", callback_data=f"admin:news_edit:{item['id']}:body")
    b.button(text="👁 Превью",
             callback_data=f"admin:news_preview:{item['id']}")
    b.button(text="📢 Разослать всем",
             callback_data=f"admin:news_cast:{item['id']}")
    b.button(text="🗑 Удалить",
             callback_data=f"admin:news_del:{item['id']}")
    b.button(text="⬅️ К списку", callback_data="admin:news:0")
    b.adjust(1, 2, 2, 2, 1, 1)
    return text, b.as_markup()


@router.callback_query(F.data.startswith("admin:news_view:"))
async def cb_admin_news_view(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    nid = int(c.data.split(":")[2])
    text, kb = await _admin_news_card(nid)
    if not text:
        await c.answer("Новость не найдена", show_alert=True)
        return
    with suppress(TelegramAPIError):
        await c.message.edit_text(text, reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("admin:news_pub:"))
async def cb_admin_news_pub(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    nid = int(c.data.split(":")[2])
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE news SET is_published = 1 WHERE id = ?", (nid,)
        )
        await conn.commit()
    await c.answer("Опубликовано")
    await cb_admin_news_view(c)


@router.callback_query(F.data.startswith("admin:news_unpub:"))
async def cb_admin_news_unpub(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    nid = int(c.data.split(":")[2])
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE news SET is_published = 0 WHERE id = ?", (nid,)
        )
        await conn.commit()
    await c.answer("Снято с публикации")
    await cb_admin_news_view(c)


@router.callback_query(F.data.startswith("admin:news_pin:"))
async def cb_admin_news_pin(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    nid = int(c.data.split(":")[2])
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("UPDATE news SET is_pinned = 1 WHERE id = ?", (nid,))
        await conn.commit()
    await c.answer("Закреплено")
    await cb_admin_news_view(c)


@router.callback_query(F.data.startswith("admin:news_unpin:"))
async def cb_admin_news_unpin(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    nid = int(c.data.split(":")[2])
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("UPDATE news SET is_pinned = 0 WHERE id = ?", (nid,))
        await conn.commit()
    await c.answer("Откреплено")
    await cb_admin_news_view(c)


@router.callback_query(F.data.startswith("admin:news_preview:"))
async def cb_admin_news_preview(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    nid = int(c.data.split(":")[2])
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        item = await (await conn.execute(
            "SELECT * FROM news WHERE id = ?", (nid,)
        )).fetchone()
    if not item:
        await c.answer("Не найдено", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ К новости", callback_data=f"admin:news_view:{nid}")
    b.adjust(1)
    await _send_news_post(c.from_user.id, item, reply_markup=b.as_markup())
    await c.answer("Превью отправлено")


@router.callback_query(F.data.startswith("admin:news_del:"))
async def cb_admin_news_del(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    parts = c.data.split(":")
    nid = int(parts[2])
    if len(parts) >= 4 and parts[3] == "yes":
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute("DELETE FROM news WHERE id = ?", (nid,))
            await conn.commit()
        await c.answer("Удалено")
        # вернуться к списку
        c.data = "admin:news:0"
        # FSM не задействован
        from aiogram.fsm.context import FSMContext as _FSM  # noqa: F401
        # пересобрать список вручную: используем edit_text
        async with aiosqlite.connect(DB_PATH) as conn:
            conn.row_factory = aiosqlite.Row
            total = (await (await conn.execute(
                "SELECT COUNT(*) AS c FROM news"
            )).fetchone())["c"]
            rows = await (await conn.execute(
                "SELECT id, title, is_published, is_pinned, broadcast_done "
                "FROM news ORDER BY is_pinned DESC, id DESC LIMIT ?",
                (NEWS_PAGE,),
            )).fetchall()
        text = f"<b>📰 Новости (всего: {total})</b>"
        if total == 0:
            text += "\n\nЕщё ничего не добавлено."
        b = InlineKeyboardBuilder()
        for r in rows:
            flags = []
            if r["is_pinned"]:
                flags.append("📌")
            flags.append("🟢" if r["is_published"] else "⚪️")
            if r["broadcast_done"]:
                flags.append("📢")
            b.button(
                text=f"{''.join(flags)} #{r['id']} {r['title'][:30]}",
                callback_data=f"admin:news_view:{r['id']}",
            )
        b.adjust(1)
        tail = InlineKeyboardBuilder()
        tail.button(text="➕ Новая новость", callback_data="admin:news_new")
        tail.button(text="⬅️ Админ", callback_data="admin:menu")
        tail.adjust(1)
        b.attach(tail)
        with suppress(TelegramAPIError):
            await c.message.edit_text(text, reply_markup=b.as_markup())
        return

    b = InlineKeyboardBuilder()
    b.button(text="🗑 Да, удалить", callback_data=f"admin:news_del:{nid}:yes")
    b.button(text="Отмена", callback_data=f"admin:news_view:{nid}")
    b.adjust(1)
    with suppress(TelegramAPIError):
        await c.message.edit_text(
            f"Удалить новость #{nid}? Это действие необратимо.",
            reply_markup=b.as_markup(),
        )
    await c.answer()


@router.callback_query(F.data.startswith("admin:news_edit:"))
async def cb_admin_news_edit(c: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(c.from_user.id):
        return
    _, _, nid_raw, field = c.data.split(":")
    nid = int(nid_raw)
    if field not in ("title", "body"):
        await c.answer("Поле недоступно", show_alert=True)
        return
    await state.set_state(NewsSG.edit_value)
    await state.update_data(news_id=nid, field=field)
    await c.message.edit_text(
        f"Введите новое значение для <b>{esc(field)}</b>:",
        reply_markup=kb_back(f"admin:news_view:{nid}"),
    )
    await c.answer()


@router.message(NewsSG.edit_value)
async def admin_news_edit_value(m: Message, state: FSMContext) -> None:
    if not is_admin(m.from_user.id):
        return
    data = await state.get_data()
    await state.clear()
    nid = int(data.get("news_id", 0))
    field = data.get("field", "")
    if field == "title":
        value = (m.text or "").strip()[:120]
        if not value:
            await m.answer("Пусто.")
            return
    elif field == "body":
        value = (m.html_text or m.text or "").strip()[:3500]
    else:
        return
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            f"UPDATE news SET {field} = ? WHERE id = ?", (value, nid)
        )
        await conn.commit()
    text, kb = await _admin_news_card(nid)
    if text:
        await m.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("admin:news_cast:"))
async def cb_admin_news_cast(c: CallbackQuery) -> None:
    if not is_admin(c.from_user.id):
        return
    nid = int(c.data.split(":")[2])
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        item = await (await conn.execute(
            "SELECT * FROM news WHERE id = ?", (nid,)
        )).fetchone()
        if not item:
            await c.answer("Не найдено", show_alert=True)
            return
        if not item["is_published"]:
            await c.answer("Сначала опубликуйте новость", show_alert=True)
            return
        users = await (await conn.execute(
            "SELECT tg_id FROM users"
        )).fetchall()
    await c.answer("Запускаю рассылку…")
    with suppress(TelegramAPIError):
        await c.message.edit_text(
            f"📢 Рассылка новости <b>#{nid}</b> по <b>{len(users)}</b> "
            f"пользователям. Это займёт ~{len(users) * 0.05:.0f} с.",
            reply_markup=None,
        )
    asyncio.create_task(_run_news_broadcast(c.from_user.id, dict(item),
                                            [int(u["tg_id"]) for u in users]))


async def _run_news_broadcast(admin_id: int, item: dict,
                              uids: list[int]) -> None:
    sent, blocked, failed = 0, 0, 0
    # обернуть item в подобие Row через простой объект
    class _Row(dict):
        def __getitem__(self, k):  # type: ignore[override]
            return dict.__getitem__(self, k)
    row = _Row(item)
    for uid in uids:
        try:
            await _send_news_post(uid, row, reply_markup=None)
            sent += 1
        except TelegramForbiddenError:
            blocked += 1
        except TelegramAPIError:
            failed += 1
        await asyncio.sleep(0.05)
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE news SET broadcast_done = 1 WHERE id = ?", (item["id"],)
        )
        await conn.commit()
    with suppress(TelegramAPIError):
        await bot.send_message(
            admin_id,
            f"📢 Рассылка новости #{item['id']} завершена.\n"
            f"Доставлено: <b>{sent}</b>, заблокировали бот: <b>{blocked}</b>, "
            f"ошибок: <b>{failed}</b>.",
            reply_markup=kb_back("admin:news:0"),
        )


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
