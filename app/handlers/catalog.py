"""Catalog browsing — categories → products → product card."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy import select

from app.db import Database
from app.i18n import Translator
from app.keyboards import back_to_menu, categories_kb, product_card_kb, products_kb
from app.locales import t as lt
from app.models import Category, Product
from app.services import stock_for
from app.utils import format_price, safe_html

router = Router(name="catalog")


def _delivery_label(locale: str, dt: str) -> str:
    return {
        "key": lt(locale, "admin.p.type_key"),
        "file": lt(locale, "admin.p.type_file"),
        "text": lt(locale, "admin.p.type_text"),
        "subscription": lt(locale, "admin.p.type_sub"),
    }.get(dt, dt)


@router.callback_query(F.data == "menu:catalog")
async def cb_catalog(c: CallbackQuery, t: Translator, db: Database) -> None:
    async with db.session() as session:
        cats = await session.scalars(
            select(Category)
            .where(Category.is_active.is_(True))
            .order_by(Category.sort_order, Category.id)
        )
        items = [(c.id, c.title) for c in cats]
    if not items:
        await c.message.edit_text(t("catalog.empty"), reply_markup=back_to_menu(t.locale))
        await c.answer()
        return
    await c.message.edit_text(
        t("catalog.title"), reply_markup=categories_kb(t.locale, items)
    )
    await c.answer()


@router.callback_query(F.data.startswith("cat:"))
async def cb_category(c: CallbackQuery, t: Translator, db: Database) -> None:
    cat_id = int(c.data.split(":")[1])
    async with db.session() as session:
        cat = await session.get(Category, cat_id)
        if cat is None or not cat.is_active:
            await c.answer()
            return
        prods = await session.scalars(
            select(Product)
            .where(Product.category_id == cat_id, Product.is_active.is_(True))
            .order_by(Product.id)
        )
        items = [
            (p.id, p.title, format_price(p.price, p.currency)) for p in prods
        ]
    if not items:
        await c.message.edit_text(
            t("catalog.cat_empty"), reply_markup=back_to_menu(t.locale)
        )
        await c.answer()
        return
    await c.message.edit_text(
        t("catalog.cat_title", title=safe_html(cat.title)),
        reply_markup=products_kb(t.locale, items),
    )
    await c.answer()


@router.callback_query(F.data.startswith("prod:"))
async def cb_product(c: CallbackQuery, t: Translator, db: Database) -> None:
    pid = int(c.data.split(":")[1])
    async with db.session() as session:
        product = await session.get(Product, pid)
        if product is None or not product.is_active:
            await c.answer()
            return
        stock = await stock_for(session, product)
    in_stock = stock is None or stock > 0
    stock_label = t("catalog.unlimited") if stock is None else str(stock)
    text = t(
        "catalog.product_card",
        title=safe_html(product.title),
        description=safe_html(product.description) or "—",
        price=format_price(product.price, product.currency),
        stock=stock_label,
        delivery=_delivery_label(t.locale, product.delivery_type),
    )
    if not in_stock:
        text = text + "\n\n" + t("catalog.out_of_stock")
    if product.photo_file_id:
        try:
            await c.message.delete()
        except Exception:
            pass
        await c.message.answer_photo(
            product.photo_file_id,
            caption=text,
            reply_markup=product_card_kb(t.locale, product.id, in_stock=in_stock),
        )
    else:
        await c.message.edit_text(
            text, reply_markup=product_card_kb(t.locale, product.id, in_stock=in_stock)
        )
    await c.answer()
