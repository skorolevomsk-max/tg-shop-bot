"""Bot bootstrap: build dispatcher, run polling."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
)

from app.config import Settings, load_settings
from app.db import Database
from app.handlers import build_main_router
from app.i18n import I18nMiddleware


async def setup_commands(bot: Bot, settings: Settings) -> None:
    base = [
        BotCommand(command="start", description="Главное меню / Main menu"),
        BotCommand(command="help", description="Помощь / Help"),
        BotCommand(command="cancel", description="Отменить / Cancel"),
    ]
    admin_extra = base + [BotCommand(command="admin", description="Админ-панель / Admin")]
    await bot.set_my_commands(base, scope=BotCommandScopeDefault())
    for admin_id in settings.admin_ids:
        try:
            await bot.set_my_commands(admin_extra, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception:  # noqa: BLE001
            pass


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    settings = load_settings()

    db = Database(settings)
    await db.init_models()

    bot = Bot(
        settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(I18nMiddleware(settings, db))
    dp.include_router(build_main_router())

    await setup_commands(bot, settings)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        await db.close()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
