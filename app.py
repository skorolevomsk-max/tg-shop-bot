"""
FastAPI-обёртка для деплоя Telegram-бота на Fly.io через `devin deploy backend`.

Сам бот использует long-polling (см. bot.py). FastAPI здесь — только для
health-чека, Fly.io и Devin deploy требуют HTTP-сервер на $PORT.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

import bot as shop_bot

log = logging.getLogger("shop-bot.app")


async def _run_polling() -> None:
    """Запускает aiogram polling. Ошибки логируются, задача может быть отменена."""
    try:
        await shop_bot.init_db()
        await shop_bot.set_bot_commands()
        log.info("Bot polling starting…")
        await shop_bot.dp.start_polling(
            shop_bot.bot,
            allowed_updates=shop_bot.dp.resolve_used_update_types(),
        )
    except asyncio.CancelledError:
        log.info("Bot polling cancelled")
        raise
    except Exception:
        log.exception("Bot polling crashed")
        raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_run_polling())
    try:
        yield
    finally:
        task.cancel()
        with_suppress = (asyncio.CancelledError, Exception)
        try:
            await task
        except with_suppress:
            pass
        # корректно закрываем bot session
        try:
            await shop_bot.bot.session.close()
        except Exception:
            log.exception("bot session close failed")


app = FastAPI(lifespan=lifespan, title="tg-shop-bot")


@app.get("/")
async def root() -> dict:
    return {"ok": True, "service": "tg-shop-bot"}


@app.get("/health")
async def health() -> dict:
    return {"ok": True}
