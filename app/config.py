"""Configuration loaded from environment / .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _parse_admin_ids(raw: str) -> set[int]:
    out: set[int] = set()
    for chunk in raw.replace(" ", "").split(","):
        if not chunk:
            continue
        try:
            out.add(int(chunk))
        except ValueError:
            continue
    return out


def _bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name, "").strip().lower()
    if not val:
        return default
    return val in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_ids: set[int]
    log_channel_id: int
    default_locale: str
    currency: str
    db_path: str

    # Payment providers
    telegram_payments_token: str
    yookassa_shop_id: str
    yookassa_secret_key: str
    yookassa_return_url: str
    crypto_pay_token: str
    crypto_pay_testnet: bool
    crypto_pay_asset: str

    # Storage paths
    storage_dir: Path = field(default=Path("storage"))

    @property
    def has_telegram_payments(self) -> bool:
        return bool(self.telegram_payments_token)

    @property
    def has_yookassa(self) -> bool:
        return bool(self.yookassa_shop_id and self.yookassa_secret_key)

    @property
    def has_cryptobot(self) -> bool:
        return bool(self.crypto_pay_token)

    @property
    def crypto_pay_base(self) -> str:
        return (
            "https://testnet-pay.crypt.bot/api"
            if self.crypto_pay_testnet
            else "https://pay.crypt.bot/api"
        )

    @property
    def db_url(self) -> str:
        # SQLAlchemy async URL for aiosqlite
        return f"sqlite+aiosqlite:///{self.db_path}"


def load_settings() -> Settings:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise SystemExit("BOT_TOKEN is not set in .env")

    admin_ids = _parse_admin_ids(os.getenv("ADMIN_IDS", ""))
    if not admin_ids:
        raise SystemExit("ADMIN_IDS is not set (comma-separated tg ids)")

    locale = os.getenv("DEFAULT_LOCALE", "ru").strip().lower()
    if locale not in {"ru", "en"}:
        locale = "ru"

    settings = Settings(
        bot_token=bot_token,
        admin_ids=admin_ids,
        log_channel_id=int(os.getenv("LOG_CHANNEL_ID", "0") or 0),
        default_locale=locale,
        currency=os.getenv("CURRENCY", "RUB").strip().upper() or "RUB",
        db_path=os.getenv("DB_PATH", "shop.db").strip() or "shop.db",
        telegram_payments_token=os.getenv("TELEGRAM_PAYMENTS_TOKEN", "").strip(),
        yookassa_shop_id=os.getenv("YOOKASSA_SHOP_ID", "").strip(),
        yookassa_secret_key=os.getenv("YOOKASSA_SECRET_KEY", "").strip(),
        yookassa_return_url=os.getenv("YOOKASSA_RETURN_URL", "https://t.me/").strip(),
        crypto_pay_token=os.getenv("CRYPTO_PAY_TOKEN", "").strip(),
        crypto_pay_testnet=_bool("CRYPTO_PAY_TESTNET", False),
        crypto_pay_asset=os.getenv("CRYPTO_PAY_ASSET", "USDT").strip().upper() or "USDT",
    )
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    return settings
