"""i18n helper bound to a user's selected locale, resolved on every event."""

from __future__ import annotations

from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from aiogram.types import User as TgUser
from sqlalchemy import select

from app.config import Settings
from app.db import Database
from app.locales import t as _t
from app.models import User


class Translator:
    def __init__(self, locale: str) -> None:
        self.locale = locale

    def __call__(self, key: str, /, **fmt: object) -> str:
        return _t(self.locale, key, **fmt)


class I18nMiddleware(BaseMiddleware):
    """Loads/creates the user, resolves locale, injects helpers into handlers."""

    def __init__(self, settings: Settings, database: Database) -> None:
        self.settings = settings
        self.database = database

    async def __call__(self, handler, event: TelegramObject, data: dict[str, Any]):
        tg_user: TgUser | None = data.get("event_from_user")
        locale = self.settings.default_locale
        user_obj: User | None = None
        if tg_user is not None and not tg_user.is_bot:
            async with self.database.session() as session:
                user_obj = await session.scalar(
                    select(User).where(User.tg_id == tg_user.id)
                )
                if user_obj is None:
                    detected = (tg_user.language_code or "").lower()
                    initial = "en" if detected.startswith("en") else self.settings.default_locale
                    user_obj = User(
                        tg_id=tg_user.id,
                        username=tg_user.username,
                        full_name=tg_user.full_name,
                        locale=initial,
                    )
                    session.add(user_obj)
                else:
                    # keep username/name fresh
                    user_obj.username = tg_user.username
                    user_obj.full_name = tg_user.full_name
                locale = user_obj.locale or locale

        data["locale"] = locale
        data["t"] = Translator(locale)
        data["settings"] = self.settings
        data["db"] = self.database
        data["is_admin"] = (
            tg_user is not None and tg_user.id in self.settings.admin_ids
        )
        return await handler(event, data)
