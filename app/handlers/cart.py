"""Cart management — add/remove/clear/view."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.db import Database
from app.i18n import Translator
from app.keyboards import back_to_menu, cart_kb
from app.services import add_to_cart, clear_cart, get_cart, remove_cart_item, stock_for
from app.utils import format_price, safe_html

router = Router(name="cart")


@router.callback_query(F.data.startswith("cart:add:"))
async def cb_cart_add(c: CallbackQuery, t: Translator, db: Database) -> None:
    pid = int(c.data.split(":")[2])
    async with db.session() as session:
        from app.models import Product

        product = await session.get(Product, pid)
        if not product or not product.is_active:
            await c.answer()
            return
        stock = await stock_for(session, product)
        if stock is not None and stock < 1:
            await c.answer(t("catalog.out_of_stock"), show_alert=True)
            return
        await add_to_cart(session, c.from_user.id, pid)
    await c.answer(t("catalog.added"), show_alert=False)


@router.callback_query(F.data == "menu:cart")
async def cb_cart_show(c: CallbackQuery, t: Translator, db: Database) -> None:
    await _show_cart(c, t, db)


async def _show_cart(c: CallbackQuery, t: Translator, db: Database) -> None:
    async with db.session() as session:
        items = await get_cart(session, c.from_user.id)
        currency = items[0][1].currency if items else "RUB"
    if not items:
        await c.message.edit_text(t("cart.empty"), reply_markup=back_to_menu(t.locale))
        await c.answer()
        return
    total = 0
    lines: list[str] = [t("cart.title")]
    kb_items: list[tuple[int, str]] = []
    for ci, product in items:
        line_total = product.price * ci.quantity
        total += line_total
        lines.append(
            t(
                "cart.line",
                title=safe_html(product.title),
                qty=ci.quantity,
                sum=format_price(line_total, product.currency),
            )
        )
        kb_items.append((ci.id, product.title))
    lines.append("")
    lines.append(t("cart.total", total=format_price(total, currency)))
    await c.message.edit_text("\n".join(lines), reply_markup=cart_kb(t.locale, kb_items))
    await c.answer()


@router.callback_query(F.data.startswith("cart:rm:"))
async def cb_cart_remove(c: CallbackQuery, t: Translator, db: Database) -> None:
    cart_id = int(c.data.split(":")[2])
    async with db.session() as session:
        await remove_cart_item(session, c.from_user.id, cart_id)
    await c.answer(t("cart.removed"))
    await _show_cart(c, t, db)


@router.callback_query(F.data == "cart:clear")
async def cb_cart_clear(c: CallbackQuery, t: Translator, db: Database) -> None:
    async with db.session() as session:
        await clear_cart(session, c.from_user.id)
    await c.answer(t("cart.cleared"))
    await c.message.edit_text(t("cart.empty"), reply_markup=back_to_menu(t.locale))
