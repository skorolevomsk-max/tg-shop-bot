"""Common handlers — /start, main menu, language switch, help, my orders, my subs."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message
from sqlalchemy import desc, select

from app.db import Database
from app.i18n import Translator
from app.keyboards import back_to_menu, language_kb, main_menu
from app.models import Order, User
from app.services import list_user_subscriptions
from app.utils import format_price

router = Router(name="common")


async def show_menu(target: Message | CallbackQuery, t: Translator, *, is_admin: bool) -> None:
    text = t("menu.title")
    kb = main_menu(t.locale, is_admin=is_admin)
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=kb)
        await target.answer()
    else:
        await target.answer(text, reply_markup=kb)


@router.message(CommandStart())
async def cmd_start(m: Message, t: Translator, is_admin: bool) -> None:
    await show_menu(m, t, is_admin=is_admin)


@router.message(Command("help"))
async def cmd_help(m: Message, t: Translator) -> None:
    await m.answer(t("help.text"), reply_markup=back_to_menu(t.locale))


@router.callback_query(F.data == "menu:home")
async def cb_menu(c: CallbackQuery, t: Translator, is_admin: bool) -> None:
    await show_menu(c, t, is_admin=is_admin)


@router.callback_query(F.data == "menu:help")
async def cb_help(c: CallbackQuery, t: Translator) -> None:
    await c.message.edit_text(t("help.text"), reply_markup=back_to_menu(t.locale))
    await c.answer()


@router.callback_query(F.data == "menu:lang")
async def cb_lang_menu(c: CallbackQuery, t: Translator) -> None:
    await c.message.edit_text(t("lang.choose"), reply_markup=language_kb(t.locale))
    await c.answer()


@router.callback_query(F.data.startswith("lang:set:"))
async def cb_lang_set(c: CallbackQuery, db: Database, is_admin: bool) -> None:
    new_locale = c.data.split(":")[-1]
    if new_locale not in {"ru", "en"}:
        await c.answer()
        return
    async with db.session() as session:
        user = await session.get(User, c.from_user.id)
        if user is None:
            user = User(tg_id=c.from_user.id, locale=new_locale)
            session.add(user)
        else:
            user.locale = new_locale
    # rebuild menu in the new locale
    new_t = Translator(new_locale)
    await c.message.edit_text(new_t("lang.changed"), reply_markup=main_menu(new_locale, is_admin=is_admin))
    await c.answer()


@router.callback_query(F.data == "menu:orders")
async def cb_orders(c: CallbackQuery, t: Translator, db: Database) -> None:
    async with db.session() as session:
        rows = await session.scalars(
            select(Order)
            .where(Order.user_id == c.from_user.id)
            .order_by(desc(Order.created_at))
            .limit(20)
        )
        orders = list(rows)
    if not orders:
        await c.message.edit_text(t("orders.empty"), reply_markup=back_to_menu(t.locale))
        await c.answer()
        return
    lines = [t("orders.title")]
    for o in orders:
        status = t(f"orders.status.{o.status}") if o.status else o.status
        lines.append(
            t(
                "orders.line",
                id=o.id,
                date=o.created_at.strftime("%Y-%m-%d %H:%M"),
                total=format_price(o.total, o.currency),
                status=status,
            )
        )
    await c.message.edit_text("\n".join(lines), reply_markup=back_to_menu(t.locale))
    await c.answer()


@router.callback_query(F.data == "menu:subs")
async def cb_subs(c: CallbackQuery, t: Translator, db: Database) -> None:
    async with db.session() as session:
        items = await list_user_subscriptions(session, c.from_user.id)
    if not items:
        await c.message.edit_text(t("subs.empty"), reply_markup=back_to_menu(t.locale))
        await c.answer()
        return
    lines = [t("subs.title")]
    for sub, product in items:
        lines.append(
            t(
                "subs.line",
                title=product.title,
                until=sub.expires_at.strftime("%Y-%m-%d %H:%M UTC"),
            )
        )
    await c.message.edit_text("\n".join(lines), reply_markup=back_to_menu(t.locale))
    await c.answer()
