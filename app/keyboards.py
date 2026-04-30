"""Inline keyboards builders."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.locales import t


def main_menu(locale: str, *, is_admin: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=t(locale, "menu.catalog"), callback_data="menu:catalog")
    b.button(text=t(locale, "menu.cart"), callback_data="menu:cart")
    b.button(text=t(locale, "menu.orders"), callback_data="menu:orders")
    b.button(text=t(locale, "menu.subs"), callback_data="menu:subs")
    b.button(text=t(locale, "menu.lang"), callback_data="menu:lang")
    b.button(text=t(locale, "menu.help"), callback_data="menu:help")
    if is_admin:
        b.button(text=t(locale, "menu.admin"), callback_data="admin:home")
    b.adjust(2, 2, 2, 1)
    return b.as_markup()


def language_kb(locale: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=t(locale, "lang.ru"), callback_data="lang:set:ru")
    b.button(text=t(locale, "lang.en"), callback_data="lang:set:en")
    b.button(text=t(locale, "btn.menu"), callback_data="menu:home")
    b.adjust(2, 1)
    return b.as_markup()


def back_to_menu(locale: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=t(locale, "btn.menu"), callback_data="menu:home")
    return b.as_markup()


def cancel_kb(locale: str, *, target: str = "menu:home") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=t(locale, "btn.cancel"), callback_data=target)
    return b.as_markup()


def categories_kb(locale: str, items: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for cid, title in items:
        b.button(text=title, callback_data=f"cat:{cid}")
    b.button(text=t(locale, "btn.menu"), callback_data="menu:home")
    b.adjust(1)
    return b.as_markup()


def products_kb(
    locale: str, items: list[tuple[int, str, str]]
) -> InlineKeyboardMarkup:
    """items: (product_id, title, price_str)"""
    b = InlineKeyboardBuilder()
    for pid, title, price_str in items:
        b.button(text=f"{title} — {price_str}", callback_data=f"prod:{pid}")
    b.button(text=t(locale, "btn.back"), callback_data="menu:catalog")
    b.adjust(1)
    return b.as_markup()


def product_card_kb(
    locale: str, product_id: int, *, in_stock: bool
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if in_stock:
        b.button(text=t(locale, "catalog.add_cart"), callback_data=f"cart:add:{product_id}")
        b.button(text=t(locale, "catalog.buy_now"), callback_data=f"buy:{product_id}")
    b.button(text=t(locale, "btn.back"), callback_data="menu:catalog")
    b.adjust(2, 1)
    return b.as_markup()


def cart_kb(
    locale: str, items: list[tuple[int, str]]
) -> InlineKeyboardMarkup:
    """items: (cart_item_id, title)"""
    b = InlineKeyboardBuilder()
    for cart_id, title in items:
        b.button(text=t(locale, "cart.remove", title=title), callback_data=f"cart:rm:{cart_id}")
    b.button(text=t(locale, "cart.checkout"), callback_data="cart:checkout")
    b.button(text=t(locale, "cart.clear"), callback_data="cart:clear")
    b.button(text=t(locale, "btn.menu"), callback_data="menu:home")
    b.adjust(1)
    return b.as_markup()


def checkout_methods_kb(
    locale: str,
    order_id: int,
    *,
    has_tg: bool,
    has_yk: bool,
    has_crypto: bool,
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if has_tg:
        b.button(text=t(locale, "checkout.tg"), callback_data=f"pay:tg:{order_id}")
    if has_yk:
        b.button(text=t(locale, "checkout.yk"), callback_data=f"pay:yk:{order_id}")
    if has_crypto:
        b.button(text=t(locale, "checkout.crypto"), callback_data=f"pay:crypto:{order_id}")
    b.button(text=t(locale, "checkout.cancel"), callback_data=f"order:cancel:{order_id}")
    b.adjust(1)
    return b.as_markup()


def payment_link_kb(
    locale: str, order_id: int, pay_url: str, provider: str
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text=t(locale, "checkout.pay_link"), url=pay_url))
    b.button(
        text=t(locale, "checkout.check"),
        callback_data=f"pay:check:{provider}:{order_id}",
    )
    b.button(text=t(locale, "checkout.cancel"), callback_data=f"order:cancel:{order_id}")
    b.adjust(1)
    return b.as_markup()
