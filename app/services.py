"""Business-logic services — orders, cart, delivery."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    DELIVERY_FILE,
    DELIVERY_KEY,
    DELIVERY_SUBSCRIPTION,
    DELIVERY_TEXT,
    ORDER_DELIVERED,
    ORDER_PAID,
    CartItem,
    KeyStock,
    Order,
    OrderItem,
    Product,
    Subscription,
    User,
    utcnow,
)

log = logging.getLogger(__name__)


async def add_to_cart(session: AsyncSession, user_id: int, product_id: int) -> None:
    existing = await session.scalar(
        select(CartItem).where(
            CartItem.user_id == user_id, CartItem.product_id == product_id
        )
    )
    if existing:
        existing.quantity += 1
    else:
        session.add(CartItem(user_id=user_id, product_id=product_id, quantity=1))


async def get_cart(
    session: AsyncSession, user_id: int
) -> list[tuple[CartItem, Product]]:
    rows = await session.execute(
        select(CartItem, Product)
        .join(Product, Product.id == CartItem.product_id)
        .where(CartItem.user_id == user_id)
        .order_by(CartItem.id)
    )
    return list(rows.all())


async def clear_cart(session: AsyncSession, user_id: int) -> None:
    items = await session.scalars(
        select(CartItem).where(CartItem.user_id == user_id)
    )
    for it in items:
        await session.delete(it)


async def remove_cart_item(session: AsyncSession, user_id: int, cart_id: int) -> None:
    item = await session.scalar(
        select(CartItem).where(CartItem.id == cart_id, CartItem.user_id == user_id)
    )
    if item:
        await session.delete(item)


async def stock_for(session: AsyncSession, product: Product) -> int | None:
    """How many units left. None means unlimited (file/text/subscription)."""
    if product.delivery_type == DELIVERY_KEY:
        n = await session.scalar(
            select(func.count())
            .select_from(KeyStock)
            .where(KeyStock.product_id == product.id, KeyStock.is_used.is_(False))
        )
        return int(n or 0)
    return None


async def create_order_from_cart(
    session: AsyncSession, user_id: int, currency: str
) -> Order | None:
    cart = await get_cart(session, user_id)
    if not cart:
        return None
    order = Order(user_id=user_id, total=0, currency=currency)
    session.add(order)
    await session.flush()
    total = 0
    for ci, product in cart:
        if not product.is_active:
            continue
        # Check key stock availability up-front
        if product.delivery_type == DELIVERY_KEY:
            stock = await stock_for(session, product)
            if stock is None or stock < ci.quantity:
                # Skip out-of-stock items rather than crashing
                continue
        line_total = product.price * ci.quantity
        total += line_total
        session.add(
            OrderItem(
                order_id=order.id,
                product_id=product.id,
                title=product.title,
                delivery_type=product.delivery_type,
                unit_price=product.price,
                quantity=ci.quantity,
            )
        )
    if total <= 0:
        await session.delete(order)
        return None
    order.total = total
    # Clear cart
    for ci, _ in cart:
        await session.delete(ci)
    return order


async def create_order_for_product(
    session: AsyncSession, user_id: int, product: Product, currency: str
) -> Order | None:
    if not product.is_active:
        return None
    if product.delivery_type == DELIVERY_KEY:
        stock = await stock_for(session, product)
        if stock is None or stock < 1:
            return None
    order = Order(user_id=user_id, total=product.price, currency=currency)
    session.add(order)
    await session.flush()
    session.add(
        OrderItem(
            order_id=order.id,
            product_id=product.id,
            title=product.title,
            delivery_type=product.delivery_type,
            unit_price=product.price,
            quantity=1,
        )
    )
    return order


async def deliver_order(
    session: AsyncSession, order: Order
) -> list[tuple[OrderItem, Product | None, str | None, str | None]]:
    """Mark the order as delivered. Returns per-item delivery info:
    (item, product_or_none, payload_text, key_value)
    For DELIVERY_FILE the caller will look up product.delivery_payload (file_id)
    and the corresponding photo/document field. We pass back the product so
    callers can do whatever they need.
    """
    out: list[tuple[OrderItem, Product | None, str | None, str | None]] = []
    items = await session.scalars(
        select(OrderItem).where(OrderItem.order_id == order.id)
    )
    for item in items:
        product: Product | None = None
        if item.product_id:
            product = await session.get(Product, item.product_id)

        if item.delivery_type == DELIVERY_KEY and product is not None:
            for _ in range(item.quantity):
                key = await session.scalar(
                    select(KeyStock)
                    .where(
                        KeyStock.product_id == product.id,
                        KeyStock.is_used.is_(False),
                    )
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                if key is None:
                    out.append((item, product, None, None))
                    continue
                key.is_used = True
                key.used_by = order.user_id
                key.used_at = utcnow()
                item.delivered_payload = (
                    (item.delivered_payload or "") + ("\n" if item.delivered_payload else "") + key.value
                )
                out.append((item, product, None, key.value))

        elif item.delivery_type == DELIVERY_TEXT and product is not None:
            text = product.delivery_payload or ""
            item.delivered_payload = text
            out.append((item, product, text, None))

        elif item.delivery_type == DELIVERY_FILE and product is not None:
            file_id = product.delivery_payload or ""
            item.delivered_payload = file_id
            out.append((item, product, file_id, None))

        elif item.delivery_type == DELIVERY_SUBSCRIPTION and product is not None:
            try:
                days = int(product.delivery_payload or "30")
            except ValueError:
                days = 30
            sub = await session.scalar(
                select(Subscription).where(
                    Subscription.user_id == order.user_id,
                    Subscription.product_id == product.id,
                )
            )
            now = utcnow()
            if sub and sub.expires_at > now:
                sub.expires_at = sub.expires_at + timedelta(days=days)
                expires = sub.expires_at
            else:
                if sub:
                    sub.expires_at = now + timedelta(days=days)
                    expires = sub.expires_at
                else:
                    expires = now + timedelta(days=days)
                    session.add(
                        Subscription(
                            user_id=order.user_id,
                            product_id=product.id,
                            started_at=now,
                            expires_at=expires,
                            order_id=order.id,
                        )
                    )
            iso = expires.replace(tzinfo=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            item.delivered_payload = iso
            out.append((item, product, iso, None))

        else:
            out.append((item, product, None, None))

    order.status = ORDER_DELIVERED if all(x[2] is not None or x[3] is not None for x in out) else ORDER_PAID
    order.delivered_at = utcnow() if order.status == ORDER_DELIVERED else order.delivered_at
    return out


async def list_user_subscriptions(
    session: AsyncSession, user_id: int
) -> list[tuple[Subscription, Product]]:
    rows = await session.execute(
        select(Subscription, Product)
        .join(Product, Product.id == Subscription.product_id)
        .where(
            Subscription.user_id == user_id,
            Subscription.expires_at > datetime.now(timezone.utc),
        )
        .order_by(Subscription.expires_at)
    )
    return list(rows.all())


async def get_or_create_user(
    session: AsyncSession, tg_id: int, username: str | None, full_name: str | None,
    default_locale: str
) -> User:
    user = await session.get(User, tg_id)
    if user is None:
        user = User(
            tg_id=tg_id,
            username=username,
            full_name=full_name,
            locale=default_locale,
        )
        session.add(user)
    return user
