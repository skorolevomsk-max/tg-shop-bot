"""Checkout, payment provider selection and post-payment delivery."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.types import (
    CallbackQuery,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from sqlalchemy import select

from app.config import Settings
from app.db import Database
from app.i18n import Translator
from app.keyboards import back_to_menu, checkout_methods_kb, payment_link_kb
from app.models import (
    DELIVERY_FILE,
    DELIVERY_KEY,
    DELIVERY_SUBSCRIPTION,
    DELIVERY_TEXT,
    ORDER_CANCELED,
    ORDER_PAID,
    Order,
    OrderItem,
    Product,
    User,
    utcnow,
)
from app.payments import CryptoBotProvider, TelegramPaymentsProvider, YooKassaProvider
from app.services import (
    create_order_for_product,
    create_order_from_cart,
    deliver_order,
)
from app.utils import format_price, safe_html

router = Router(name="checkout")
log = logging.getLogger(__name__)


def _providers(settings: Settings) -> tuple[
    TelegramPaymentsProvider, YooKassaProvider, CryptoBotProvider
]:
    return (
        TelegramPaymentsProvider(settings.telegram_payments_token),
        YooKassaProvider(
            settings.yookassa_shop_id,
            settings.yookassa_secret_key,
            settings.yookassa_return_url,
        ),
        CryptoBotProvider(
            settings.crypto_pay_token,
            settings.crypto_pay_base,
            settings.crypto_pay_asset,
        ),
    )


def _order_summary(t: Translator, order: Order, items: list[OrderItem]) -> str:
    lines = []
    for it in items:
        lines.append(
            f"• {safe_html(it.title)} × {it.quantity} — "
            f"<b>{format_price(it.unit_price * it.quantity, order.currency)}</b>"
        )
    return t(
        "checkout.title",
        id=order.id,
        lines="\n".join(lines),
        total=format_price(order.total, order.currency),
    )


async def _send_checkout(
    target: CallbackQuery, settings: Settings, t: Translator, db: Database, order: Order
) -> None:
    tg, yk, crypto = _providers(settings)
    async with db.session() as session:
        items = list(
            await session.scalars(select(OrderItem).where(OrderItem.order_id == order.id))
        )
    text = _order_summary(t, order, items)
    has_any = tg.enabled or yk.enabled or crypto.enabled
    if not has_any:
        await target.message.answer(t("checkout.no_methods"), reply_markup=back_to_menu(t.locale))
        return
    kb = checkout_methods_kb(
        t.locale,
        order.id,
        has_tg=tg.enabled,
        has_yk=yk.enabled,
        has_crypto=crypto.enabled,
    )
    await target.message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "cart:checkout")
async def cb_cart_checkout(
    c: CallbackQuery, t: Translator, db: Database, settings: Settings
) -> None:
    async with db.session() as session:
        order = await create_order_from_cart(session, c.from_user.id, settings.currency)
    if order is None:
        await c.answer(t("cart.empty"), show_alert=True)
        return
    await c.answer()
    await _send_checkout(c, settings, t, db, order)


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy_now(
    c: CallbackQuery, t: Translator, db: Database, settings: Settings
) -> None:
    pid = int(c.data.split(":")[1])
    async with db.session() as session:
        product = await session.get(Product, pid)
        if not product:
            await c.answer()
            return
        order = await create_order_for_product(session, c.from_user.id, product, settings.currency)
    if order is None:
        await c.answer(t("catalog.out_of_stock"), show_alert=True)
        return
    await c.answer()
    await _send_checkout(c, settings, t, db, order)


@router.callback_query(F.data.startswith("order:cancel:"))
async def cb_cancel_order(
    c: CallbackQuery, t: Translator, db: Database
) -> None:
    order_id = int(c.data.split(":")[2])
    async with db.session() as session:
        order = await session.get(Order, order_id)
        if order and order.user_id == c.from_user.id and order.status == "pending":
            order.status = ORDER_CANCELED
    await c.message.edit_text(t("checkout.canceled"), reply_markup=back_to_menu(t.locale))
    await c.answer()


# ---------- Telegram Payments ----------


@router.callback_query(F.data.startswith("pay:tg:"))
async def cb_pay_tg(
    c: CallbackQuery, t: Translator, db: Database, settings: Settings, bot: Bot
) -> None:
    if not settings.has_telegram_payments:
        await c.answer(t("checkout.no_methods"), show_alert=True)
        return
    order_id = int(c.data.split(":")[2])
    async with db.session() as session:
        order = await session.get(Order, order_id)
        if order is None or order.user_id != c.from_user.id:
            await c.answer()
            return
        items = list(
            await session.scalars(select(OrderItem).where(OrderItem.order_id == order.id))
        )
        order.payment_provider = "telegram"
        order.payment_id = f"tg:{order.id}"
    prices = [
        LabeledPrice(label=it.title[:32] or "Item", amount=it.unit_price * it.quantity)
        for it in items
    ]
    title = f"Order #{order.id}"
    description = ", ".join(it.title for it in items)[:255] or title
    try:
        await bot.send_invoice(
            chat_id=c.from_user.id,
            title=title,
            description=description,
            payload=f"order:{order.id}",
            provider_token=settings.telegram_payments_token,
            currency=order.currency,
            prices=prices,
            start_parameter=f"order_{order.id}",
        )
    except Exception as e:
        log.exception("send_invoice failed: %s", e)
        await c.message.answer(t("checkout.error"))
        await c.answer()
        return
    await c.answer()


@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery, bot: Bot) -> None:
    await bot.answer_pre_checkout_query(q.id, ok=True)


@router.message(F.successful_payment)
async def successful_payment(
    m: Message, t: Translator, db: Database, bot: Bot, settings: Settings
) -> None:
    sp = m.successful_payment
    payload = sp.invoice_payload or ""
    if not payload.startswith("order:"):
        return
    try:
        order_id = int(payload.split(":")[1])
    except ValueError:
        return
    await _mark_paid_and_deliver(bot, db, settings, t, order_id, m.from_user.id)


# ---------- YooKassa & CryptoBot via URL ----------


async def _create_external(
    settings: Settings, db: Database, order_id: int, user_id: int, provider: str
) -> tuple[Order | None, str | None]:
    tg, yk, crypto = _providers(settings)
    async with db.session() as session:
        order = await session.get(Order, order_id)
        if not order or order.user_id != user_id:
            return None, None
        items = list(
            await session.scalars(select(OrderItem).where(OrderItem.order_id == order.id))
        )
        description = f"Order #{order.id}: " + ", ".join(it.title for it in items)
        try:
            if provider == "yk":
                if not yk.enabled:
                    return order, None
                res = await yk.create(
                    amount_minor=order.total,
                    currency=order.currency,
                    order_id=order.id,
                    description=description,
                )
                order.payment_provider = "yookassa"
            elif provider == "crypto":
                if not crypto.enabled:
                    return order, None
                res = await crypto.create(
                    amount_minor=order.total,
                    currency=order.currency,
                    order_id=order.id,
                    description=description,
                )
                order.payment_provider = "cryptobot"
            else:
                return order, None
        except Exception as e:
            log.exception("create payment failed: %s", e)
            return order, None
        order.payment_id = res.payment_id
        order.payment_url = res.pay_url
    return order, res.pay_url


@router.callback_query(F.data.startswith("pay:yk:"))
async def cb_pay_yk(
    c: CallbackQuery, t: Translator, db: Database, settings: Settings
) -> None:
    if not settings.has_yookassa:
        await c.answer(t("checkout.no_methods"), show_alert=True)
        return
    order_id = int(c.data.split(":")[2])
    order, pay_url = await _create_external(settings, db, order_id, c.from_user.id, "yk")
    if order is None or not pay_url:
        await c.answer(t("checkout.error"), show_alert=True)
        return
    await c.message.answer(
        t("checkout.invoice_caption", id=order.id),
        reply_markup=payment_link_kb(t.locale, order.id, pay_url, "yk"),
    )
    await c.answer()


@router.callback_query(F.data.startswith("pay:crypto:"))
async def cb_pay_crypto(
    c: CallbackQuery, t: Translator, db: Database, settings: Settings
) -> None:
    if not settings.has_cryptobot:
        await c.answer(t("checkout.no_methods"), show_alert=True)
        return
    order_id = int(c.data.split(":")[2])
    order, pay_url = await _create_external(settings, db, order_id, c.from_user.id, "crypto")
    if order is None or not pay_url:
        await c.answer(t("checkout.error"), show_alert=True)
        return
    await c.message.answer(
        t("checkout.invoice_caption", id=order.id),
        reply_markup=payment_link_kb(t.locale, order.id, pay_url, "crypto"),
    )
    await c.answer()


@router.callback_query(F.data.startswith("pay:check:"))
async def cb_pay_check(
    c: CallbackQuery, t: Translator, db: Database, settings: Settings, bot: Bot
) -> None:
    parts = c.data.split(":")
    provider = parts[2]
    order_id = int(parts[3])
    tg, yk, crypto = _providers(settings)
    async with db.session() as session:
        order = await session.get(Order, order_id)
        if not order or order.user_id != c.from_user.id:
            await c.answer()
            return
        if order.status not in ("pending",):
            await c.answer(t("orders.status." + order.status), show_alert=True)
            return
        if provider == "yk" and order.payment_id and yk.enabled:
            paid = await yk.is_paid(order.payment_id)
        elif provider == "crypto" and order.payment_id and crypto.enabled:
            paid = await crypto.is_paid(order.payment_id)
        else:
            paid = False
    if not paid:
        await c.answer(t("checkout.not_paid"), show_alert=True)
        return
    await _mark_paid_and_deliver(bot, db, settings, t, order_id, c.from_user.id)
    await c.answer()


# ---------- Mark paid + deliver ----------


async def _mark_paid_and_deliver(
    bot: Bot,
    db: Database,
    settings: Settings,
    t: Translator,
    order_id: int,
    user_id: int,
) -> None:
    async with db.session() as session:
        order = await session.get(Order, order_id)
        if order is None or order.user_id != user_id:
            return
        if order.status not in ("pending", "paid"):
            return
        if order.status == "pending":
            order.status = ORDER_PAID
            order.paid_at = utcnow()
        deliveries = await deliver_order(session, order)
        # Capture user locale
        user = await session.get(User, user_id)
        user_locale = user.locale if user else settings.default_locale
        order_total = order.total
        order_currency = order.currency
    # Send confirmation + each item
    user_t = Translator(user_locale)
    await bot.send_message(user_id, user_t("delivery.success"))
    for item, _product, payload, key in deliveries:
        title = safe_html(item.title)
        if item.delivery_type == DELIVERY_KEY:
            if key:
                await bot.send_message(
                    user_id, user_t("delivery.key", title=title, key=safe_html(key))
                )
            else:
                await bot.send_message(user_id, user_t("delivery.no_keys"))
        elif item.delivery_type == DELIVERY_TEXT:
            await bot.send_message(
                user_id, user_t("delivery.text", title=title, text=safe_html(payload or ""))
            )
        elif item.delivery_type == DELIVERY_FILE:
            file_id = payload or ""
            try:
                await bot.send_document(
                    user_id, file_id, caption=user_t("delivery.file_caption", title=title)
                )
            except Exception:
                # Fallback: try as photo / video
                try:
                    await bot.send_photo(
                        user_id, file_id, caption=user_t("delivery.file_caption", title=title)
                    )
                except Exception as e:
                    log.warning("file delivery failed: %s", e)
                    await bot.send_message(user_id, user_t("delivery.no_keys"))
        elif item.delivery_type == DELIVERY_SUBSCRIPTION:
            await bot.send_message(
                user_id,
                user_t("delivery.subscription", title=title, until=safe_html(payload or "")),
            )

    # Notify admins
    if settings.log_channel_id:
        try:
            await bot.send_message(
                settings.log_channel_id,
                f"💰 Order #{order_id} paid by {user_id}: "
                f"{format_price(order_total, order_currency)}",
            )
        except Exception:
            pass
