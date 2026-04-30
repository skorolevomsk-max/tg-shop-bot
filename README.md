# tg-shop-bot — Telegram-бот для продажи цифровых товаров

Модульный Telegram-бот для продажи цифровых товаров: ключи/коды, файлы,
тексты/инструкции и подписки. Поддерживает три способа оплаты, а интерфейс
переключается между русским и английским языками.

- 🛒 **Каталог** — категории и товары
- 🧺 **Корзина** и оформление заказа
- 💳 **Оплата**: Telegram Payments, YooKassa, CryptoBot (пользователь выбирает)
- 🚚 **Автодоставка** после оплаты:
  - 🔑 ключи/коды из пула
  - 📎 файлы (Telegram file_id)
  - 📄 текст/инструкция
  - 🔓 подписка на N дней
- 🛠 Админ-панель прямо в боте: категории/товары/ключи/статистика/рассылка
- 🌐 Многоязычность: 🇷🇺 русский + 🇬🇧 английский с переключателем

## Стек

- Python 3.10+
- aiogram 3.26
- SQLAlchemy 2 (async, aiosqlite)
- Telegram Payments / YooKassa REST API / Crypto Pay API

## Структура проекта

```
app/
  config.py            # настройки из .env
  db.py                # async SQLAlchemy engine
  models.py            # ORM-модели
  i18n.py / locales.py # переводы RU/EN + middleware
  keyboards.py         # инлайн-клавиатуры
  states.py            # FSM состояния (админка)
  services.py          # бизнес-логика: корзина/заказы/доставка
  utils.py             # форматирование цен и т.п.
  main.py              # запуск бота
  handlers/
    common.py          # /start, меню, язык, мои заказы/подписки
    catalog.py         # каталог
    cart.py            # корзина
    checkout.py        # оплата + доставка
    admin.py           # админ-панель
  payments/
    base.py            # интерфейс провайдера
    telegram_pay.py    # Telegram Payments
    yookassa.py        # YooKassa REST
    cryptobot.py       # Crypto Pay API
bot.py                 # точка входа: python bot.py
```

## Установка (локально)

```bash
git clone https://github.com/skorolevomsk-max/tg-shop-bot.git
cd tg-shop-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# отредактируйте .env
python bot.py
```

## Запуск в Docker

```bash
cp .env.example .env  # отредактируйте перед запуском
docker compose up -d --build
```

База SQLite хранится в `./storage/shop.db` — томе, который монтируется в
контейнер. Резервируйте этот файл вместе с `.env`.

## Конфигурация (.env)

| Ключ | Назначение |
| --- | --- |
| `BOT_TOKEN` | токен бота от @BotFather |
| `ADMIN_IDS` | tg id админов через запятую |
| `LOG_CHANNEL_ID` | (опц.) канал/чат для логов оплат |
| `DEFAULT_LOCALE` | `ru` или `en` (по умолчанию `ru`) |
| `CURRENCY` | ISO-код валюты (`RUB`, `USD`, ...) |
| `DB_PATH` | путь к SQLite-файлу |
| `TELEGRAM_PAYMENTS_TOKEN` | provider-токен Telegram Payments (необяз.) |
| `YOOKASSA_SHOP_ID` / `YOOKASSA_SECRET_KEY` | креды YooKassa (необяз.) |
| `YOOKASSA_RETURN_URL` | URL возврата после оплаты YooKassa |
| `CRYPTO_PAY_TOKEN` | токен Crypto Pay App (необяз.) |
| `CRYPTO_PAY_TESTNET` | `true`/`false` |
| `CRYPTO_PAY_ASSET` | базовый актив CryptoBot, напр. `USDT` |

Если какой-то из платёжных провайдеров не настроен — бот просто скрывает
эту кнопку на чекауте.

## Команды

- `/start` — главное меню
- `/help` — краткая справка
- `/admin` — админ-панель (только для `ADMIN_IDS`)
- `/cancel` — выйти из текущего диалога

## Как добавить товар

1. `/admin` → 📂 Категории → ➕ Категория — создать категорию.
2. `/admin` → 📦 Товары → ➕ Товар — пройти мастер:
   - выбрать категорию
   - название, описание (или `/skip`), цена
   - тип доставки:
     - **Ключи/коды** — после создания товара пришлите ключи (по одному на
       строку, текстом или `.txt` файлом) — бот сложит их в пул.
     - **Файл** — пришлите документ/фото/видео; бот запомнит file_id и
       будет отправлять покупателю.
     - **Текст** — пришлите текст/инструкцию.
     - **Подписка** — введите количество дней, на которые активируется
       подписка после оплаты.

## Платёжные потоки

- **Telegram Payments** — нативный инвойс через `bot.send_invoice`.
  Автоматическая выдача товара по `successful_payment`.
- **YooKassa** — создаёт платёж через REST `POST /v3/payments`, отдаёт
  пользователю ссылку. Кнопка «🔄 Проверить оплату» опрашивает
  `GET /v3/payments/{id}` и при `succeeded` запускает выдачу.
- **CryptoBot** — `createInvoice` через Crypto Pay API в фиате с
  `accepted_assets=USDT,TON,BTC,ETH,...`. Проверка — `getInvoices`.

## Деплой

- Любой Linux-хост с Python 3.10+ или Docker.
- Запускайте `python bot.py` через systemd / pm2 / tmux.
- Бэкапьте `storage/shop.db` и `.env`.

## Лицензия

MIT
