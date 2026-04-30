"""Admin panel: categories, products, key stock, stats, broadcast."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    Document,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select

from app.config import Settings
from app.db import Database
from app.i18n import Translator
from app.keyboards import back_to_menu, cancel_kb
from app.locales import t as lt
from app.models import (
    DELIVERY_FILE,
    DELIVERY_KEY,
    DELIVERY_SUBSCRIPTION,
    DELIVERY_TEXT,
    ORDER_DELIVERED,
    ORDER_PAID,
    Category,
    KeyStock,
    Order,
    Product,
    User,
)
from app.states import AdminBroadcastSG, AdminCategorySG, AdminKeysSG, AdminProductSG
from app.utils import format_price, safe_html

router = Router(name="admin")
log = logging.getLogger(__name__)


def _is_admin(settings: Settings, user_id: int) -> bool:
    return user_id in settings.admin_ids


def admin_home_kb(locale: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=lt(locale, "admin.products"), callback_data="adm:p:list")
    b.button(text=lt(locale, "admin.cats"), callback_data="adm:c:list")
    b.button(text=lt(locale, "admin.stats"), callback_data="adm:stats")
    b.button(text=lt(locale, "admin.broadcast"), callback_data="adm:bc")
    b.button(text=lt(locale, "btn.menu"), callback_data="menu:home")
    b.adjust(2, 2, 1)
    return b.as_markup()


@router.message(Command("admin"))
async def cmd_admin(m: Message, t: Translator, settings: Settings) -> None:
    if not _is_admin(settings, m.from_user.id):
        await m.answer(t("admin.no_access"))
        return
    await m.answer(t("admin.title"), reply_markup=admin_home_kb(t.locale))


@router.callback_query(F.data == "admin:home")
async def cb_admin_home(c: CallbackQuery, t: Translator, settings: Settings) -> None:
    if not _is_admin(settings, c.from_user.id):
        await c.answer(t("admin.no_access"), show_alert=True)
        return
    await c.message.edit_text(t("admin.title"), reply_markup=admin_home_kb(t.locale))
    await c.answer()


# ---------- Categories ----------


@router.callback_query(F.data == "adm:c:list")
async def cb_cat_list(c: CallbackQuery, t: Translator, db: Database, settings: Settings) -> None:
    if not _is_admin(settings, c.from_user.id):
        return
    async with db.session() as session:
        cats = list(
            await session.scalars(select(Category).order_by(Category.sort_order, Category.id))
        )
    b = InlineKeyboardBuilder()
    for cat in cats:
        flag = "✅" if cat.is_active else "🚫"
        b.button(text=f"{flag} {cat.title}", callback_data=f"adm:c:tog:{cat.id}")
    b.button(text=lt(t.locale, "admin.cat_add"), callback_data="adm:c:add")
    b.button(text=lt(t.locale, "btn.back"), callback_data="admin:home")
    b.adjust(1)
    text = "📂 <b>Categories</b>" if t.locale == "en" else "📂 <b>Категории</b>"
    await c.message.edit_text(text, reply_markup=b.as_markup())
    await c.answer()


@router.callback_query(F.data == "adm:c:add")
async def cb_cat_add(c: CallbackQuery, state: FSMContext, t: Translator, settings: Settings) -> None:
    if not _is_admin(settings, c.from_user.id):
        return
    await state.set_state(AdminCategorySG.title)
    await c.message.edit_text(t("admin.cat_title_ask"), reply_markup=cancel_kb(t.locale, target="admin:home"))
    await c.answer()


@router.message(AdminCategorySG.title)
async def cat_title(m: Message, state: FSMContext, t: Translator, db: Database, settings: Settings) -> None:
    if not _is_admin(settings, m.from_user.id):
        return
    title = (m.text or "").strip()
    if not title:
        return
    async with db.session() as session:
        session.add(Category(title=title))
    await state.clear()
    await m.answer(t("admin.cat_added"), reply_markup=admin_home_kb(t.locale))


@router.callback_query(F.data.startswith("adm:c:tog:"))
async def cb_cat_toggle(c: CallbackQuery, db: Database, settings: Settings) -> None:
    if not _is_admin(settings, c.from_user.id):
        return
    cat_id = int(c.data.split(":")[3])
    async with db.session() as session:
        cat = await session.get(Category, cat_id)
        if cat:
            cat.is_active = not cat.is_active
    await c.answer("OK")
    await cb_cat_list(c, Translator(c.from_user.language_code or "ru"), db, settings)  # refresh


# ---------- Products ----------


@router.callback_query(F.data == "adm:p:list")
async def cb_p_list(c: CallbackQuery, t: Translator, db: Database, settings: Settings) -> None:
    if not _is_admin(settings, c.from_user.id):
        return
    async with db.session() as session:
        products = list(
            await session.scalars(select(Product).order_by(Product.id.desc()).limit(30))
        )
    b = InlineKeyboardBuilder()
    if not products:
        text = t("admin.p.list_empty")
    else:
        text = "<b>Products</b>" if t.locale == "en" else "<b>Товары</b>"
        for p in products:
            tag = "✅" if p.is_active else "🚫"
            b.button(
                text=f"{tag} {p.title} — {format_price(p.price, p.currency)}",
                callback_data=f"adm:p:view:{p.id}",
            )
    b.button(text=lt(t.locale, "admin.product_add"), callback_data="adm:p:add")
    b.button(text=lt(t.locale, "btn.back"), callback_data="admin:home")
    b.adjust(1)
    await c.message.edit_text(text, reply_markup=b.as_markup())
    await c.answer()


@router.callback_query(F.data.startswith("adm:p:view:"))
async def cb_p_view(c: CallbackQuery, t: Translator, db: Database, settings: Settings) -> None:
    if not _is_admin(settings, c.from_user.id):
        return
    pid = int(c.data.split(":")[3])
    async with db.session() as session:
        product = await session.get(Product, pid)
        if not product:
            await c.answer()
            return
        cat = await session.get(Category, product.category_id)
        stock = await session.scalar(
            select(func.count())
            .select_from(KeyStock)
            .where(KeyStock.product_id == pid, KeyStock.is_used.is_(False))
        )
    text = t(
        "admin.p.detail",
        title=safe_html(product.title),
        id=product.id,
        cat=safe_html(cat.title if cat else "-"),
        price=format_price(product.price, product.currency),
        dt=product.delivery_type,
        stock=int(stock or 0) if product.delivery_type == DELIVERY_KEY else "-",
        active=t("yes") if product.is_active else t("no"),
    )
    b = InlineKeyboardBuilder()
    b.button(text=t("admin.p.toggle"), callback_data=f"adm:p:tog:{pid}")
    if product.delivery_type == DELIVERY_KEY:
        b.button(text=t("admin.p.add_keys"), callback_data=f"adm:p:keys:{pid}")
    b.button(text=t("admin.p.delete"), callback_data=f"adm:p:del:{pid}")
    b.button(text=lt(t.locale, "btn.back"), callback_data="adm:p:list")
    b.adjust(1)
    await c.message.edit_text(text, reply_markup=b.as_markup())
    await c.answer()


@router.callback_query(F.data.startswith("adm:p:tog:"))
async def cb_p_toggle(c: CallbackQuery, t: Translator, db: Database, settings: Settings) -> None:
    if not _is_admin(settings, c.from_user.id):
        return
    pid = int(c.data.split(":")[3])
    async with db.session() as session:
        product = await session.get(Product, pid)
        if product:
            product.is_active = not product.is_active
    await c.answer(t("admin.p.toggled"))
    c.data = f"adm:p:view:{pid}"
    await cb_p_view(c, t, db, settings)


@router.callback_query(F.data.startswith("adm:p:del:"))
async def cb_p_delete(c: CallbackQuery, t: Translator, db: Database, settings: Settings) -> None:
    if not _is_admin(settings, c.from_user.id):
        return
    pid = int(c.data.split(":")[3])
    async with db.session() as session:
        product = await session.get(Product, pid)
        if product:
            await session.delete(product)
    await c.answer(t("admin.p.deleted"))
    c.data = "adm:p:list"
    await cb_p_list(c, t, db, settings)


# ---- Add product flow ----


@router.callback_query(F.data == "adm:p:add")
async def cb_p_add(c: CallbackQuery, state: FSMContext, t: Translator, db: Database, settings: Settings) -> None:
    if not _is_admin(settings, c.from_user.id):
        return
    async with db.session() as session:
        cats = list(await session.scalars(select(Category).where(Category.is_active.is_(True))))
    if not cats:
        await c.answer(t("admin.no_cats"), show_alert=True)
        return
    b = InlineKeyboardBuilder()
    for cat in cats:
        b.button(text=cat.title, callback_data=f"adm:pa:cat:{cat.id}")
    b.button(text=lt(t.locale, "btn.cancel"), callback_data="adm:p:list")
    b.adjust(1)
    await state.set_state(AdminProductSG.category)
    await c.message.edit_text(t("admin.pick_cat"), reply_markup=b.as_markup())
    await c.answer()


@router.callback_query(AdminProductSG.category, F.data.startswith("adm:pa:cat:"))
async def p_pick_cat(c: CallbackQuery, state: FSMContext, t: Translator) -> None:
    cat_id = int(c.data.split(":")[3])
    await state.update_data(category_id=cat_id)
    await state.set_state(AdminProductSG.title)
    await c.message.edit_text(t("admin.p.title_ask"), reply_markup=cancel_kb(t.locale, target="admin:home"))
    await c.answer()


@router.message(AdminProductSG.title)
async def p_title(m: Message, state: FSMContext, t: Translator) -> None:
    title = (m.text or "").strip()
    if not title:
        return
    await state.update_data(title=title)
    await state.set_state(AdminProductSG.description)
    await m.answer(t("admin.p.desc_ask"), reply_markup=cancel_kb(t.locale, target="admin:home"))


@router.message(AdminProductSG.description)
async def p_desc(m: Message, state: FSMContext, t: Translator, settings: Settings) -> None:
    text = m.text or ""
    desc = "" if text.strip() == "/skip" else text.strip()
    await state.update_data(description=desc)
    await state.set_state(AdminProductSG.price)
    await m.answer(
        t("admin.p.price_ask", currency=settings.currency),
        reply_markup=cancel_kb(t.locale, target="admin:home"),
    )


@router.message(AdminProductSG.price)
async def p_price(m: Message, state: FSMContext, t: Translator) -> None:
    raw = (m.text or "").replace(",", ".").strip()
    try:
        amount = float(raw)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await m.answer(t("admin.p.invalid_price"))
        return
    await state.update_data(price_major=amount)
    await state.set_state(AdminProductSG.delivery_type)

    b = InlineKeyboardBuilder()
    b.button(text=t("admin.p.type_key"), callback_data="adm:pa:dt:key")
    b.button(text=t("admin.p.type_file"), callback_data="adm:pa:dt:file")
    b.button(text=t("admin.p.type_text"), callback_data="adm:pa:dt:text")
    b.button(text=t("admin.p.type_sub"), callback_data="adm:pa:dt:sub")
    b.button(text=lt(t.locale, "btn.cancel"), callback_data="admin:home")
    b.adjust(1)
    await m.answer(t("admin.p.type_ask"), reply_markup=b.as_markup())


@router.callback_query(AdminProductSG.delivery_type, F.data.startswith("adm:pa:dt:"))
async def p_dt(c: CallbackQuery, state: FSMContext, t: Translator) -> None:
    dt = c.data.split(":")[3]
    mapping = {
        "key": DELIVERY_KEY,
        "file": DELIVERY_FILE,
        "text": DELIVERY_TEXT,
        "sub": DELIVERY_SUBSCRIPTION,
    }
    delivery_type = mapping.get(dt)
    if not delivery_type:
        await c.answer()
        return
    await state.update_data(delivery_type=delivery_type)
    await state.set_state(AdminProductSG.payload)
    if delivery_type == DELIVERY_FILE:
        prompt = t("admin.p.payload_file")
    elif delivery_type == DELIVERY_TEXT:
        prompt = t("admin.p.payload_text")
    elif delivery_type == DELIVERY_SUBSCRIPTION:
        prompt = t("admin.p.payload_sub")
    else:
        prompt = t("admin.p.payload_key")
    await c.message.edit_text(prompt, reply_markup=cancel_kb(t.locale, target="admin:home"))
    await c.answer()


def _create_product(
    *,
    session,
    data: dict,
    settings: Settings,
    delivery_payload: str | None,
    photo_file_id: str | None = None,
) -> Product:
    from app.utils import to_minor_units

    price_minor = to_minor_units(float(data["price_major"]), settings.currency)
    product = Product(
        category_id=int(data["category_id"]),
        title=str(data["title"]),
        description=str(data.get("description") or ""),
        price=price_minor,
        currency=settings.currency,
        delivery_type=str(data["delivery_type"]),
        delivery_payload=delivery_payload,
        photo_file_id=photo_file_id,
    )
    session.add(product)
    return product


@router.message(AdminProductSG.payload)
async def p_payload(
    m: Message, state: FSMContext, t: Translator, db: Database, settings: Settings
) -> None:
    data = await state.get_data()
    dt = data.get("delivery_type")

    if dt == DELIVERY_FILE:
        # Accept document/photo/video
        file_id = None
        if m.document:
            file_id = m.document.file_id
        elif m.photo:
            file_id = m.photo[-1].file_id
        elif m.video:
            file_id = m.video.file_id
        elif m.audio:
            file_id = m.audio.file_id
        if not file_id:
            await m.answer(t("admin.p.payload_file"))
            return
        async with db.session() as session:
            product = _create_product(
                session=session,
                data=data,
                settings=settings,
                delivery_payload=file_id,
            )
            await session.flush()
            pid = product.id
        await state.clear()
        await m.answer(t("admin.p.created", id=pid), reply_markup=admin_home_kb(t.locale))
        return

    text = (m.text or "").strip()
    if dt == DELIVERY_TEXT:
        if not text:
            await m.answer(t("admin.p.payload_text"))
            return
        async with db.session() as session:
            product = _create_product(
                session=session, data=data, settings=settings, delivery_payload=text
            )
            await session.flush()
            pid = product.id
        await state.clear()
        await m.answer(t("admin.p.created", id=pid), reply_markup=admin_home_kb(t.locale))
        return

    if dt == DELIVERY_SUBSCRIPTION:
        try:
            days = int(text)
            if days <= 0:
                raise ValueError
        except ValueError:
            await m.answer(t("admin.p.invalid_days"))
            return
        async with db.session() as session:
            product = _create_product(
                session=session,
                data=data,
                settings=settings,
                delivery_payload=str(days),
            )
            await session.flush()
            pid = product.id
        await state.clear()
        await m.answer(t("admin.p.created", id=pid), reply_markup=admin_home_kb(t.locale))
        return

    if dt == DELIVERY_KEY:
        # Create product first, then enter key upload state
        async with db.session() as session:
            product = _create_product(
                session=session, data=data, settings=settings, delivery_payload=None
            )
            await session.flush()
            pid = product.id
        await state.clear()
        await m.answer(t("admin.p.created", id=pid))
        # transition into AdminKeysSG
        await state.set_state(AdminKeysSG.waiting)
        await state.update_data(product_id=pid)
        await m.answer(
            t("admin.keys.upload_ask"),
            reply_markup=cancel_kb(t.locale, target="admin:home"),
        )
        return


# ---------- Keys upload ----------


@router.callback_query(F.data.startswith("adm:p:keys:"))
async def cb_keys_start(c: CallbackQuery, state: FSMContext, t: Translator, settings: Settings) -> None:
    if not _is_admin(settings, c.from_user.id):
        return
    pid = int(c.data.split(":")[3])
    await state.set_state(AdminKeysSG.waiting)
    await state.update_data(product_id=pid)
    await c.message.answer(
        t("admin.keys.upload_ask"),
        reply_markup=cancel_kb(t.locale, target="admin:home"),
    )
    await c.answer()


@router.message(AdminKeysSG.waiting, F.document)
async def keys_upload_doc(
    m: Message, state: FSMContext, t: Translator, db: Database, bot: Bot
) -> None:
    data = await state.get_data()
    pid = int(data.get("product_id") or 0)
    if not pid:
        await state.clear()
        return
    doc: Document = m.document
    file = await bot.get_file(doc.file_id)
    raw = await bot.download_file(file.file_path)
    try:
        content = raw.read().decode("utf-8", errors="ignore")
    except Exception as e:
        log.warning("decode keys file: %s", e)
        return
    n = await _store_keys(db, pid, content)
    await m.answer(t("admin.keys.added", n=n), reply_markup=admin_home_kb(t.locale))
    await state.clear()


@router.message(AdminKeysSG.waiting, F.text)
async def keys_upload_text(
    m: Message, state: FSMContext, t: Translator, db: Database
) -> None:
    data = await state.get_data()
    pid = int(data.get("product_id") or 0)
    if not pid:
        await state.clear()
        return
    n = await _store_keys(db, pid, m.text or "")
    await m.answer(t("admin.keys.added", n=n), reply_markup=admin_home_kb(t.locale))
    await state.clear()


async def _store_keys(db: Database, product_id: int, content: str) -> int:
    keys = [k.strip() for k in content.splitlines() if k.strip()]
    if not keys:
        return 0
    async with db.session() as session:
        for value in keys:
            session.add(KeyStock(product_id=product_id, value=value))
    return len(keys)


# ---------- Stats ----------


@router.callback_query(F.data == "adm:stats")
async def cb_stats(
    c: CallbackQuery, t: Translator, db: Database, settings: Settings
) -> None:
    if not _is_admin(settings, c.from_user.id):
        return
    async with db.session() as session:
        users_n = await session.scalar(select(func.count()).select_from(User))
        orders_n = await session.scalar(select(func.count()).select_from(Order))
        paid_n = await session.scalar(
            select(func.count())
            .select_from(Order)
            .where(Order.status.in_([ORDER_PAID, ORDER_DELIVERED]))
        )
        revenue = await session.scalar(
            select(func.coalesce(func.sum(Order.total), 0)).where(
                Order.status.in_([ORDER_PAID, ORDER_DELIVERED])
            )
        )
        products_n = await session.scalar(
            select(func.count()).select_from(Product).where(Product.is_active.is_(True))
        )
    text = t(
        "admin.stats.text",
        users=int(users_n or 0),
        orders=int(orders_n or 0),
        paid=int(paid_n or 0),
        revenue=format_price(int(revenue or 0), settings.currency),
        products=int(products_n or 0),
    )
    await c.message.edit_text(text, reply_markup=back_to_menu(t.locale))
    await c.answer()


# ---------- Broadcast ----------


@router.callback_query(F.data == "adm:bc")
async def cb_bc_start(
    c: CallbackQuery, state: FSMContext, t: Translator, settings: Settings
) -> None:
    if not _is_admin(settings, c.from_user.id):
        return
    await state.set_state(AdminBroadcastSG.waiting)
    await c.message.edit_text(
        t("admin.broadcast.ask"), reply_markup=cancel_kb(t.locale, target="admin:home")
    )
    await c.answer()


@router.message(AdminBroadcastSG.waiting)
async def bc_capture(m: Message, state: FSMContext, t: Translator, settings: Settings) -> None:
    if not _is_admin(settings, m.from_user.id):
        return
    await state.update_data(chat_id=m.chat.id, message_id=m.message_id)
    await state.set_state(AdminBroadcastSG.confirm)
    b = InlineKeyboardBuilder()
    b.button(text=t("yes"), callback_data="adm:bc:go")
    b.button(text=t("no"), callback_data="admin:home")
    b.adjust(2)
    await m.answer(t("admin.broadcast.confirm"), reply_markup=b.as_markup())


@router.callback_query(AdminBroadcastSG.confirm, F.data == "adm:bc:go")
async def bc_go(
    c: CallbackQuery,
    state: FSMContext,
    t: Translator,
    db: Database,
    bot: Bot,
    settings: Settings,
) -> None:
    if not _is_admin(settings, c.from_user.id):
        return
    data = await state.get_data()
    src_chat = int(data["chat_id"])
    src_msg = int(data["message_id"])
    await state.clear()
    async with db.session() as session:
        users = list(await session.scalars(select(User).where(User.is_blocked.is_(False))))
    ok = err = 0
    for u in users:
        try:
            await bot.copy_message(chat_id=u.tg_id, from_chat_id=src_chat, message_id=src_msg)
            ok += 1
        except TelegramForbiddenError:
            async with db.session() as session:
                user = await session.get(User, u.tg_id)
                if user:
                    user.is_blocked = True
            err += 1
        except TelegramAPIError:
            err += 1
    await c.message.answer(t("admin.broadcast.done", ok=ok, err=err), reply_markup=back_to_menu(t.locale))
    await c.answer()


# ---------- Cancel any FSM ----------


@router.message(StateFilter("*"), Command("cancel"))
async def cmd_cancel(m: Message, state: FSMContext, t: Translator) -> None:
    await state.clear()
    await m.answer(t("common.canceled"))
