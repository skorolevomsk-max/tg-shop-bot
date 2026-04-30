"""Static i18n string tables (ru/en)."""

from __future__ import annotations

LOCALES: dict[str, dict[str, str]] = {
    "ru": {
        # Common
        "lang.choose": "🌐 Выберите язык:",
        "lang.changed": "✅ Язык интерфейса: Русский",
        "lang.ru": "🇷🇺 Русский",
        "lang.en": "🇬🇧 English",
        "btn.back": "⬅️ Назад",
        "btn.cancel": "Отмена",
        "btn.skip": "Пропустить",
        "btn.menu": "🏠 В меню",
        "btn.close": "Закрыть",
        "btn.continue": "Продолжить",
        "yes": "Да",
        "no": "Нет",
        # Main menu
        "menu.title": (
            "👋 <b>Добро пожаловать!</b>\n\n"
            "Это магазин цифровых товаров. Выберите, что хотите сделать:"
        ),
        "menu.catalog": "🛒 Каталог",
        "menu.cart": "🧺 Корзина",
        "menu.orders": "📦 Мои заказы",
        "menu.subs": "🔑 Мои подписки",
        "menu.lang": "🌐 Язык",
        "menu.help": "ℹ️ Помощь",
        "menu.admin": "🛠 Админ-панель",
        # Help
        "help.text": (
            "ℹ️ <b>Как это работает</b>\n\n"
            "1. Откройте <b>Каталог</b> и выберите товар.\n"
            "2. Добавьте в <b>Корзину</b> или сразу нажмите <b>Купить</b>.\n"
            "3. Выберите способ оплаты — оплата проходит онлайн.\n"
            "4. После оплаты бот автоматически выдаст товар "
            "(ключ, файл, текст или подписку).\n\n"
            "Вопросы — пишите администратору."
        ),
        # Catalog
        "catalog.empty": "Каталог пока пуст. Загляните позже.",
        "catalog.title": "🛒 <b>Каталог</b>\n\nВыберите категорию:",
        "catalog.cat_empty": "В этой категории пока нет товаров.",
        "catalog.cat_title": "📂 <b>{title}</b>\n\nВыберите товар:",
        "catalog.product_card": (
            "🛍 <b>{title}</b>\n\n"
            "{description}\n\n"
            "💵 Цена: <b>{price}</b>\n"
            "📦 В наличии: <b>{stock}</b>\n"
            "🚚 Тип доставки: <i>{delivery}</i>"
        ),
        "catalog.add_cart": "➕ В корзину",
        "catalog.buy_now": "💳 Купить сейчас",
        "catalog.added": "Добавлено в корзину.",
        "catalog.out_of_stock": "Этого товара сейчас нет в наличии.",
        "catalog.unlimited": "∞",
        # Cart
        "cart.empty": "🧺 Ваша корзина пуста.",
        "cart.title": "🧺 <b>Ваша корзина</b>",
        "cart.line": "• {title} × {qty} — <b>{sum}</b>",
        "cart.total": "Итого: <b>{total}</b>",
        "cart.checkout": "💳 Оформить заказ",
        "cart.clear": "🗑 Очистить",
        "cart.remove": "❌ {title}",
        "cart.cleared": "Корзина очищена.",
        "cart.removed": "Удалено из корзины.",
        # Checkout
        "checkout.title": (
            "💳 <b>Оформление заказа №{id}</b>\n\n{lines}\n\nИтого: <b>{total}</b>\n\n"
            "Выберите способ оплаты:"
        ),
        "checkout.tg": "💳 Telegram Pay",
        "checkout.yk": "🟢 YooKassa",
        "checkout.crypto": "🪙 CryptoBot",
        "checkout.no_methods": (
            "❗ Платёжные провайдеры не настроены. Обратитесь к администратору."
        ),
        "checkout.pay_link": "💳 Оплатить",
        "checkout.check": "🔄 Проверить оплату",
        "checkout.cancel": "❌ Отменить заказ",
        "checkout.invoice_caption": (
            "Заказ №{id}. Нажмите кнопку ниже, чтобы оплатить."
        ),
        "checkout.canceled": "Заказ отменён.",
        "checkout.not_paid": "Платёж ещё не получен. Попробуйте позже.",
        "checkout.error": "Не удалось создать платёж. Попробуйте ещё раз.",
        # Orders
        "orders.empty": "У вас пока нет заказов.",
        "orders.title": "📦 <b>Ваши заказы</b>",
        "orders.line": "№{id} • {date} • {total} • {status}",
        "orders.detail": (
            "📦 <b>Заказ №{id}</b>\nДата: {date}\nСтатус: <b>{status}</b>\n\n"
            "{lines}\n\nИтого: <b>{total}</b>"
        ),
        "orders.status.pending": "ожидает оплаты",
        "orders.status.paid": "оплачен",
        "orders.status.delivered": "выдан",
        "orders.status.canceled": "отменён",
        "orders.status.failed": "ошибка",
        # Delivery
        "delivery.success": "✅ Оплата получена. Спасибо!",
        "delivery.key": "🔑 Ваш ключ для <b>{title}</b>:\n<code>{key}</code>",
        "delivery.text": "📄 <b>{title}</b>\n\n{text}",
        "delivery.file_caption": "📎 <b>{title}</b>",
        "delivery.subscription": (
            "🔓 Подписка <b>{title}</b> активирована до <b>{until}</b>."
        ),
        "delivery.no_keys": (
            "⚠️ Ключи закончились. Администратор уведомлён, мы свяжемся с вами."
        ),
        # Subscriptions
        "subs.empty": "У вас нет активных подписок.",
        "subs.title": "🔑 <b>Ваши подписки</b>",
        "subs.line": "• {title} — до <b>{until}</b>",
        # Admin
        "admin.title": "🛠 <b>Админ-панель</b>",
        "admin.no_access": "Доступ запрещён.",
        "admin.products": "📦 Товары",
        "admin.cats": "📂 Категории",
        "admin.stats": "📊 Статистика",
        "admin.broadcast": "📣 Рассылка",
        "admin.cat_add": "➕ Категория",
        "admin.cat_title_ask": "Введите название категории:",
        "admin.cat_added": "Категория добавлена.",
        "admin.product_add": "➕ Товар",
        "admin.pick_cat": "Выберите категорию:",
        "admin.no_cats": "Сначала создайте хотя бы одну категорию.",
        "admin.p.title_ask": "Название товара:",
        "admin.p.desc_ask": "Описание (или /skip):",
        "admin.p.price_ask": "Цена в {currency} (число):",
        "admin.p.type_ask": "Выберите тип доставки:",
        "admin.p.type_key": "🔑 Ключи/коды",
        "admin.p.type_file": "📎 Файл",
        "admin.p.type_text": "📄 Текст/инструкция",
        "admin.p.type_sub": "🔓 Подписка (дни)",
        "admin.p.payload_file": "Пришлите файл, который будет выдаваться:",
        "admin.p.payload_text": "Пришлите текст, который будет выдаваться:",
        "admin.p.payload_sub": "Сколько дней даёт подписка? (число):",
        "admin.p.payload_key": (
            "Товар создан. Загрузите ключи: пришлите их одним сообщением "
            "(каждый ключ — отдельная строка) или загрузите .txt файл."
        ),
        "admin.p.created": "✅ Товар создан (ID {id}).",
        "admin.p.invalid_price": "Неверная цена. Введите число.",
        "admin.p.invalid_days": "Неверное число дней.",
        "admin.p.list_empty": "Товаров нет.",
        "admin.p.detail": (
            "<b>{title}</b>\nID {id}\nКатегория: {cat}\nЦена: {price}\n"
            "Тип: {dt}\nКлючей в наличии: {stock}\nАктивен: {active}"
        ),
        "admin.p.toggle": "🔁 Активировать/Скрыть",
        "admin.p.delete": "🗑 Удалить",
        "admin.p.add_keys": "➕ Загрузить ключи",
        "admin.p.deleted": "Товар удалён.",
        "admin.p.toggled": "Готово.",
        "admin.keys.added": "Загружено ключей: <b>{n}</b>",
        "admin.keys.upload_ask": (
            "Пришлите ключи: каждое сообщение/строка — один ключ. "
            "Можно текстом или .txt файлом."
        ),
        "admin.stats.text": (
            "📊 <b>Статистика</b>\n\n"
            "Пользователей: <b>{users}</b>\n"
            "Заказов всего: <b>{orders}</b>\n"
            "Оплачено: <b>{paid}</b>\n"
            "Выручка: <b>{revenue}</b>\n"
            "Товаров активных: <b>{products}</b>"
        ),
        "admin.broadcast.ask": (
            "Пришлите сообщение для рассылки (текст / медиа). "
            "Оно будет переслано всем пользователям."
        ),
        "admin.broadcast.confirm": "Разослать сообщение всем?",
        "admin.broadcast.done": "Готово. Доставлено: <b>{ok}</b>, ошибок: <b>{err}</b>",
        # Misc
        "currency.format": "{amount} {code}",
        "session.expired": "Сессия истекла, начните заново.",
        "common.ok": "Готово.",
        "common.canceled": "Отменено.",
    },
    "en": {
        "lang.choose": "🌐 Choose language:",
        "lang.changed": "✅ Language: English",
        "lang.ru": "🇷🇺 Русский",
        "lang.en": "🇬🇧 English",
        "btn.back": "⬅️ Back",
        "btn.cancel": "Cancel",
        "btn.skip": "Skip",
        "btn.menu": "🏠 Menu",
        "btn.close": "Close",
        "btn.continue": "Continue",
        "yes": "Yes",
        "no": "No",
        "menu.title": (
            "👋 <b>Welcome!</b>\n\nThis is a digital goods shop. Pick an option below:"
        ),
        "menu.catalog": "🛒 Catalog",
        "menu.cart": "🧺 Cart",
        "menu.orders": "📦 My orders",
        "menu.subs": "🔑 My subscriptions",
        "menu.lang": "🌐 Language",
        "menu.help": "ℹ️ Help",
        "menu.admin": "🛠 Admin",
        "help.text": (
            "ℹ️ <b>How it works</b>\n\n"
            "1. Open <b>Catalog</b> and pick an item.\n"
            "2. Add to <b>Cart</b> or hit <b>Buy now</b>.\n"
            "3. Pick a payment method — it's all online.\n"
            "4. After payment, the bot auto-delivers your item "
            "(key, file, text or subscription).\n\n"
            "Questions? Contact the admin."
        ),
        "catalog.empty": "Catalog is empty. Check back later.",
        "catalog.title": "🛒 <b>Catalog</b>\n\nPick a category:",
        "catalog.cat_empty": "No products in this category yet.",
        "catalog.cat_title": "📂 <b>{title}</b>\n\nPick a product:",
        "catalog.product_card": (
            "🛍 <b>{title}</b>\n\n"
            "{description}\n\n"
            "💵 Price: <b>{price}</b>\n"
            "📦 In stock: <b>{stock}</b>\n"
            "🚚 Delivery: <i>{delivery}</i>"
        ),
        "catalog.add_cart": "➕ Add to cart",
        "catalog.buy_now": "💳 Buy now",
        "catalog.added": "Added to cart.",
        "catalog.out_of_stock": "This product is out of stock.",
        "catalog.unlimited": "∞",
        "cart.empty": "🧺 Your cart is empty.",
        "cart.title": "🧺 <b>Your cart</b>",
        "cart.line": "• {title} × {qty} — <b>{sum}</b>",
        "cart.total": "Total: <b>{total}</b>",
        "cart.checkout": "💳 Checkout",
        "cart.clear": "🗑 Clear",
        "cart.remove": "❌ {title}",
        "cart.cleared": "Cart cleared.",
        "cart.removed": "Removed from cart.",
        "checkout.title": (
            "💳 <b>Order #{id}</b>\n\n{lines}\n\nTotal: <b>{total}</b>\n\nPick a payment method:"
        ),
        "checkout.tg": "💳 Telegram Pay",
        "checkout.yk": "🟢 YooKassa",
        "checkout.crypto": "🪙 CryptoBot",
        "checkout.no_methods": "❗ No payment providers configured. Contact admin.",
        "checkout.pay_link": "💳 Pay",
        "checkout.check": "🔄 Check payment",
        "checkout.cancel": "❌ Cancel order",
        "checkout.invoice_caption": "Order #{id}. Tap the button below to pay.",
        "checkout.canceled": "Order canceled.",
        "checkout.not_paid": "Payment not received yet. Try again in a moment.",
        "checkout.error": "Failed to create payment. Try again.",
        "orders.empty": "You have no orders yet.",
        "orders.title": "📦 <b>Your orders</b>",
        "orders.line": "#{id} • {date} • {total} • {status}",
        "orders.detail": (
            "📦 <b>Order #{id}</b>\nDate: {date}\nStatus: <b>{status}</b>\n\n"
            "{lines}\n\nTotal: <b>{total}</b>"
        ),
        "orders.status.pending": "awaiting payment",
        "orders.status.paid": "paid",
        "orders.status.delivered": "delivered",
        "orders.status.canceled": "canceled",
        "orders.status.failed": "failed",
        "delivery.success": "✅ Payment received. Thank you!",
        "delivery.key": "🔑 Your key for <b>{title}</b>:\n<code>{key}</code>",
        "delivery.text": "📄 <b>{title}</b>\n\n{text}",
        "delivery.file_caption": "📎 <b>{title}</b>",
        "delivery.subscription": (
            "🔓 Subscription <b>{title}</b> active until <b>{until}</b>."
        ),
        "delivery.no_keys": (
            "⚠️ Out of keys. Admin has been notified, we'll get back to you."
        ),
        "subs.empty": "You have no active subscriptions.",
        "subs.title": "🔑 <b>Your subscriptions</b>",
        "subs.line": "• {title} — until <b>{until}</b>",
        "admin.title": "🛠 <b>Admin panel</b>",
        "admin.no_access": "Access denied.",
        "admin.products": "📦 Products",
        "admin.cats": "📂 Categories",
        "admin.stats": "📊 Stats",
        "admin.broadcast": "📣 Broadcast",
        "admin.cat_add": "➕ Category",
        "admin.cat_title_ask": "Category title:",
        "admin.cat_added": "Category added.",
        "admin.product_add": "➕ Product",
        "admin.pick_cat": "Pick a category:",
        "admin.no_cats": "Create at least one category first.",
        "admin.p.title_ask": "Product title:",
        "admin.p.desc_ask": "Description (or /skip):",
        "admin.p.price_ask": "Price in {currency} (number):",
        "admin.p.type_ask": "Pick delivery type:",
        "admin.p.type_key": "🔑 Keys / codes",
        "admin.p.type_file": "📎 File",
        "admin.p.type_text": "📄 Text / instructions",
        "admin.p.type_sub": "🔓 Subscription (days)",
        "admin.p.payload_file": "Send the file to deliver:",
        "admin.p.payload_text": "Send the text to deliver:",
        "admin.p.payload_sub": "How many days does the subscription last?",
        "admin.p.payload_key": (
            "Product created. Upload keys: send them in one message "
            "(one key per line) or upload a .txt file."
        ),
        "admin.p.created": "✅ Product created (ID {id}).",
        "admin.p.invalid_price": "Invalid price. Enter a number.",
        "admin.p.invalid_days": "Invalid number of days.",
        "admin.p.list_empty": "No products.",
        "admin.p.detail": (
            "<b>{title}</b>\nID {id}\nCategory: {cat}\nPrice: {price}\n"
            "Type: {dt}\nKeys in stock: {stock}\nActive: {active}"
        ),
        "admin.p.toggle": "🔁 Activate/Hide",
        "admin.p.delete": "🗑 Delete",
        "admin.p.add_keys": "➕ Upload keys",
        "admin.p.deleted": "Product deleted.",
        "admin.p.toggled": "Done.",
        "admin.keys.added": "Keys uploaded: <b>{n}</b>",
        "admin.keys.upload_ask": (
            "Send keys: one per line. Plain text or a .txt file works."
        ),
        "admin.stats.text": (
            "📊 <b>Stats</b>\n\n"
            "Users: <b>{users}</b>\n"
            "Total orders: <b>{orders}</b>\n"
            "Paid: <b>{paid}</b>\n"
            "Revenue: <b>{revenue}</b>\n"
            "Active products: <b>{products}</b>"
        ),
        "admin.broadcast.ask": (
            "Send a message to broadcast (text / media). It will be forwarded to all users."
        ),
        "admin.broadcast.confirm": "Broadcast to everyone?",
        "admin.broadcast.done": "Done. Delivered: <b>{ok}</b>, errors: <b>{err}</b>",
        "currency.format": "{amount} {code}",
        "session.expired": "Session expired, start over.",
        "common.ok": "Done.",
        "common.canceled": "Canceled.",
    },
}


def t(locale: str, key: str, /, **fmt: object) -> str:
    table = LOCALES.get(locale) or LOCALES["ru"]
    s = table.get(key) or LOCALES["ru"].get(key) or key
    if fmt:
        try:
            return s.format(**fmt)
        except (KeyError, IndexError):
            return s
    return s
