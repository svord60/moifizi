import asyncio
import logging
import re
import os
from datetime import datetime
from decimal import Decimal

from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup,
                           InputMediaPhoto, ParseMode, CallbackQuery,
                           Message)
from aiogram.utils import executor
from telethon import TelegramClient, events
from telethon.errors import (SessionPasswordNeededError,
                             PhoneCodeExpiredError,
                             PhoneCodeInvalidError)
from telethon.sessions import StringSession
from tinydb import TinyDB, Query

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH")
USERBOT_PHONE = os.getenv("USERBOT_PHONE")
ADMIN_IDS = [int(id.strip()) for id in os.getenv("ADMIN_IDS", "").split(",") if id.strip()]
CRYPTO_BOT_TOKEN = os.getenv("CRYPTO_BOT_TOKEN")

CHANNEL_ID = -1002308793392
CHANNEL_LINK = "https://t.me/+Y88DiqFlqBRiNjIy"
SUPPORT_USERNAME = "swordSar"

# Базы данных
db = TinyDB("bot_data.json", indent=4, ensure_ascii=False)
users_table = db.table("users")
accounts_table = db.table("accounts")
orders_table = db.table("orders")
pending_payments = db.table("pending_payments")

# ==================== ИНИЦИАЛИЗАЦИЯ ====================
logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

# ==================== FSM СОСТОЯНИЯ ====================
class AddAccount(StatesGroup):
    waiting_country = State()
    waiting_type = State()
    waiting_year = State()
    waiting_price = State()
    waiting_phone = State()
    waiting_code = State()

class Broadcast(StatesGroup):
    waiting_text = State()

class TopUp(StatesGroup):
    waiting_crypto_amount = State()

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def is_admin(user_id):
    return user_id in ADMIN_IDS

def get_user(user_id):
    return users_table.get(Query().user_id == user_id)

def create_user_if_not(user_id, username=None):
    user = get_user(user_id)
    if not user:
        users_table.insert({
            "user_id": user_id,
            "username": username or "Неизвестный",
            "balance": 0.0,
            "purchases": 0,
            "created_at": datetime.now().isoformat()
        })
    else:
        users_table.update({"username": username or user.get("username", "Неизвестный")}, Query().user_id == user_id)

def format_price(price):
    return f"{Decimal(str(price)).quantize(Decimal('0.01'))} ₽"

def admin_keyboard():
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("➕ Добавить аккаунт", callback_data="admin_add"),
        InlineKeyboardButton("📊 Остатки", callback_data="admin_stock")
    )
    keyboard.add(
        InlineKeyboardButton("📢 Рассылка", callback_data="admin_broadcast"),
        InlineKeyboardButton("💳 Пополнить юзеру", callback_data="admin_topup_user")
    )
    keyboard.add(InlineKeyboardButton("🔙 Выйти", callback_data="main_menu"))
    return keyboard

def main_menu_keyboard(user_id):
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.row(
        InlineKeyboardButton("[👛] Купить аккаунт", callback_data="buy_regular"),
        InlineKeyboardButton("[👝] Аккаунт с отлегой", callback_data="buy_aged")
    )
    keyboard.row(InlineKeyboardButton("[🎩] Мой профиль", callback_data="profile"))
    keyboard.row(
        InlineKeyboardButton("[🗞] Правила", url="https://telegra.ph/JabaMarket---Politika-konfidencialnosti-02-12"),
        InlineKeyboardButton("[📓] Отзывы", url="https://t.me/c/2308793392/8548")
    )
    if is_admin(user_id):
        keyboard.row(InlineKeyboardButton("🛠 Админ-панель", callback_data="admin_panel"))
    return keyboard

async def show_main_menu(user_id):
    caption = (
        "Добро пожаловать ✈️\n\n"
        "<i>Чем мы лучше других сервисов</i>\n"
        "<blockquote>Моментальная выдача аккаунта.\n"
        "Большой ассортимент аккаунтов.\n"
        "Лучшее качество аккаунтов.</blockquote>"
    )
    
    await bot.send_photo(
        user_id,
        "https://iili.io/BQyyE22.jpg",
        caption=caption,
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(user_id)
    )

def get_country_keyboard(acc_type):
    keyboard = InlineKeyboardMarkup(row_width=2)
    accounts = accounts_table.search(
        (Query().acc_type == acc_type) & (Query().status == "ожидает")
    )
    unique_countries = list(set(a["country_code"] for a in accounts))
    
    for country in unique_countries:
        country_data = accounts_table.get(
            (Query().country_code == country) & (Query().acc_type == acc_type)
        )
        keyboard.insert(InlineKeyboardButton(
            f"{country_data['country_flag']} {country_data['country_name']}",
            callback_data=f"country_{acc_type}_{country}"
        ))
    
    keyboard.add(InlineKeyboardButton("◀️ Назад", callback_data="main_menu"))
    return keyboard

def get_years_keyboard(country_code):
    keyboard = InlineKeyboardMarkup(row_width=3)
    accounts = accounts_table.search(
        (Query().country_code == country_code) & 
        (Query().acc_type == "отлега") & 
        (Query().status == "ожидает")
    )
    years = sorted(list(set(a["year"] for a in accounts)), reverse=True)
    
    for year in years:
        keyboard.insert(InlineKeyboardButton(str(year), callback_data=f"year_{country_code}_{year}"))
    
    keyboard.add(InlineKeyboardButton("◀️ Назад", callback_data="buy_aged"))
    return keyboard

def get_available_account(acc_type, country_code, year=None):
    search_q = (Query().acc_type == acc_type) & \
               (Query().country_code == country_code) & \
               (Query().status == "ожидает")
    if year:
        search_q &= (Query().year == int(year))
    
    accounts = accounts_table.search(search_q)
    return accounts[0] if accounts else None

# ==================== ЮЗЕРБОТ ====================
main_userbot = None
userbot_clients = {}

async def start_main_userbot():
    global main_userbot
    try:
        main_userbot = TelegramClient(StringSession(), API_ID, API_HASH)
        await main_userbot.connect()
        
        if not await main_userbot.is_user_authorized():
            await main_userbot.send_code_request(USERBOT_PHONE)
            for admin_id in ADMIN_IDS:
                await bot.send_message(
                    admin_id,
                    f"🔐 <b>Регистрация юзербота</b>\n\n"
                    f"Номер: {USERBOT_PHONE}\n"
                    f"Отправь код подтверждения прямо в этот чат.",
                    parse_mode=ParseMode.HTML
                )
            return False
        else:
            logging.info("Юзербот уже авторизован")
            return True
    except Exception as e:
        logging.error(f"Main userbot error: {e}")
        return False

async def login_userbot_with_code(code):
    global main_userbot
    try:
        await main_userbot.sign_in(USERBOT_PHONE, code)
        for admin_id in ADMIN_IDS:
            await bot.send_message(admin_id, "✅ Юзербот успешно авторизован!")
        return True
    except SessionPasswordNeededError:
        for admin_id in ADMIN_IDS:
            await bot.send_message(admin_id, "⚠️ Нужен облачный пароль! Отправь его.")
        return False
    except Exception as e:
        logging.error(f"Login error: {e}")
        return False

async def create_userbot_for_account(phone, account_id):
    try:
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        
        if not await client.is_user_authorized():
            await client.send_code_request(phone)
        
        userbot_clients[phone] = {
            "client": client,
            "account_id": account_id,
            "awaiting_code": True
        }
        
        @client.on(events.NewMessage(from_users=777000))
        async def code_handler(event):
            message_text = event.message.message
            code_match = re.search(r'\b(\d{5})\b', message_text)
            if code_match:
                code = code_match.group(1)
                order = orders_table.get(
                    (Query().phone == phone) & (Query().status == "ожидает_код")
                )
                if order:
                    orders_table.update(
                        {"status": "код_получен", "code": code},
                        Query().doc_id == order.doc_id
                    )
                    await bot.send_message(
                        order["buyer_id"],
                        f"🔑 Код подтверждения: <code>{code}</code>\n"
                        f"Введите его для входа в аккаунт.",
                        parse_mode=ParseMode.HTML
                    )
                    orders_table.update(
                        {"status": "завершен"},
                        Query().doc_id == order.doc_id
                    )
        
        return client
    except Exception as e:
        logging.error(f"Create userbot error: {e}")
        return None

async def login_account_userbot(phone, code):
    if phone not in userbot_clients:
        return False
    
    client = userbot_clients[phone]["client"]
    account_id = userbot_clients[phone]["account_id"]
    
    try:
        await client.sign_in(phone, code)
        accounts_table.update({"has_session": True}, Query().doc_id == account_id)
        userbot_clients[phone]["awaiting_code"] = False
        return True
    except Exception as e:
        logging.error(f"Account login error: {e}")
        return False

# ==================== КОМАНДА /start ====================
@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    create_user_if_not(message.from_user.id, message.from_user.username)
    
    try:
        member = await bot.get_chat_member(CHANNEL_ID, message.from_user.id)
        if member.status in ['creator', 'administrator', 'member']:
            await show_main_menu(message.from_user.id)
        else:
            keyboard = InlineKeyboardMarkup(row_width=1)
            keyboard.add(InlineKeyboardButton("🔔 Подписаться", url=CHANNEL_LINK))
            keyboard.add(InlineKeyboardButton("♻️ Проверить подписку", callback_data="check_sub"))
            
            await bot.send_photo(
                message.from_user.id,
                "https://iili.io/BQyyE22.jpg",
                caption="<b>Для использования бота подпишитесь на канал!</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard
            )
    except Exception as e:
        await message.answer("❌ Ошибка проверки подписки")

# ==================== КОД ДЛЯ ЮЗЕРБОТА ====================
@dp.message_handler(lambda msg: is_admin(msg.from_user.id) and msg.text and len(msg.text) == 5 and msg.text.isdigit())
async def catch_code(message: Message):
    code = message.text
    
    if main_userbot and not await main_userbot.is_user_authorized():
        success = await login_userbot_with_code(code)
        if success:
            await message.answer("✅ Юзербот авторизован! Можно добавлять аккаунты.")
        return
    
    for phone, data in userbot_clients.items():
        if data.get("awaiting_code"):
            success = await login_account_userbot(phone, code)
            if success:
                await message.answer(f"✅ Вход выполнен для {phone}!\nАккаунт готов к продаже.")
                return

# ==================== CALLBACK ОБРАБОТЧИКИ ====================
@dp.callback_query_handler(text="check_sub")
async def check_subscription(call: CallbackQuery):
    try:
        member = await bot.get_chat_member(CHANNEL_ID, call.from_user.id)
        if member.status in ['creator', 'administrator', 'member']:
            await call.message.delete()
            await show_main_menu(call.from_user.id)
        else:
            await call.answer("❌ Вы не подписаны на канал!", show_alert=True)
    except:
        await call.answer("❌ Ошибка проверки!")
    await call.answer()

@dp.callback_query_handler(text="main_menu")
async def back_to_main(call: CallbackQuery):
    await call.message.delete()
    await show_main_menu(call.from_user.id)
    await call.answer()

@dp.callback_query_handler(text="profile")
async def show_profile(call: CallbackQuery):
    user = get_user(call.from_user.id)
    if not user:
        await call.answer("Ошибка!")
        return
    
    text = (
        "Профиль\n"
        "——————————————————\n"
        f"Имя пользователя: @{user.get('username', 'Неизвестный')}\n"
        f"Идентификатор: {call.from_user.id}\n"
        "——————————————————\n"
        f"👛 Баланс: {format_price(user.get('balance', 0))}\n"
        f"Покупок: {user.get('purchases', 0)}"
    )
    
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(InlineKeyboardButton("💳 Пополнить баланс", callback_data="top_up"))
    keyboard.add(InlineKeyboardButton("🆘 Поддержка", url=f"https://t.me/{SUPPORT_USERNAME}"))
    keyboard.add(InlineKeyboardButton("◀️ Назад", callback_data="main_menu"))
    
    await call.message.delete()
    await bot.send_photo(
        call.from_user.id,
        "https://iili.io/BZzNhN9.jpg",
        caption=text,
        reply_markup=keyboard
    )
    await call.answer()

@dp.callback_query_handler(text="top_up")
async def top_up_menu(call: CallbackQuery):
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(InlineKeyboardButton("💳 СБП (ЮMoney)", callback_data="topup_sbp"))
    keyboard.add(InlineKeyboardButton("💰 Crypto Bot", callback_data="topup_crypto"))
    keyboard.add(InlineKeyboardButton("◀️ Назад", callback_data="profile"))
    
    await call.message.delete()
    await bot.send_message(
        call.from_user.id,
        "<b>Выберите способ пополнения:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard
    )
    await call.answer()

@dp.callback_query_handler(text="topup_sbp")
async def sbp_info(call: CallbackQuery):
    text = (
        "<b>Пополнение через СБП:</b>\n\n"
        "1. Нажмите <b>Перевод по СБП</b>\n"
        "2. В поиске введите <b>ЮMoney</b>\n"
        "3. Номер кошелька: <code>+79646603227</code>\n"
        f"4. В комментарии укажите ваш ID: <code>{call.from_user.id}</code>\n"
        "5. После отправки — пришлите фото чека в поддержку"
    )
    
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("◀️ Назад", callback_data="top_up"))
    
    await call.message.delete()
    await bot.send_message(call.from_user.id, text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    await call.answer()

@dp.callback_query_handler(text="topup_crypto")
async def crypto_amount(call: CallbackQuery):
    await call.message.delete()
    await bot.send_message(call.from_user.id, "Введите сумму в рублях:")
    await TopUp.waiting_crypto_amount.set()
    await call.answer()

@dp.message_handler(state=TopUp.waiting_crypto_amount)
async def crypto_invoice(message: Message, state: FSMContext):
    try:
        amount = Decimal(message.text.replace(",", "."))
        if amount < 10:
            await message.answer("Минимальная сумма: 10 ₽")
            return
        
        invoice_url = f"https://t.me/send?start=IVtest{int(amount)}"
        
        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("💳 Оплатить", url=invoice_url))
        keyboard.add(InlineKeyboardButton("◀️ Отмена", callback_data="top_up"))
        
        await message.answer(
            f"<b>Счет на {format_price(amount)} создан!</b>\n"
            "После оплаты обратитесь в поддержку для зачисления.",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard
        )
    except:
        await message.answer("❌ Введите корректную сумму!")
    finally:
        await state.finish()

@dp.callback_query_handler(text="buy_regular")
async def buy_regular(call: CallbackQuery):
    keyboard = get_country_keyboard("обычный")
    await call.message.delete()
    await bot.send_message(
        call.from_user.id,
        "<b>Покупка аккаунта</b>\n\nВыберите страну:",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard
    )
    await call.answer()

@dp.callback_query_handler(text="buy_aged")
async def buy_aged(call: CallbackQuery):
    keyboard = InlineKeyboardMarkup(row_width=2)
    accounts = accounts_table.search(
        (Query().acc_type == "отлега") & (Query().status == "ожидает")
    )
    unique_countries = list(set(a["country_code"] for a in accounts))
    
    for country in unique_countries:
        country_data = accounts_table.get(
            (Query().country_code == country) & (Query().acc_type == "отлега")
        )
        keyboard.insert(InlineKeyboardButton(
            f"{country_data['country_flag']} {country_data['country_name']}",
            callback_data=f"aged_country_{country}"
        ))
    
    keyboard.add(InlineKeyboardButton("◀️ Назад", callback_data="main_menu"))
    
    await call.message.delete()
    await bot.send_message(
        call.from_user.id,
        "<b>Аккаунты с отлегой</b>\n\nВыберите страну:",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard
    )
    await call.answer()

@dp.callback_query_handler(lambda call: call.data.startswith("aged_country_"))
async def aged_country_select(call: CallbackQuery):
    country_code = call.data.replace("aged_country_", "")
    keyboard = get_years_keyboard(country_code)
    await call.message.delete()
    await bot.send_message(
        call.from_user.id,
        "<b>Выберите год регистрации:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard
    )
    await call.answer()

@dp.callback_query_handler(lambda call: call.data.startswith("country_"))
async def country_select(call: CallbackQuery):
    parts = call.data.split("_")
    acc_type = parts[1]
    country_code = parts[2]
    
    account = get_available_account(acc_type, country_code)
    
    if not account:
        await call.answer("❌ Нет доступных аккаунтов!", show_alert=True)
        return
    
    text = (
        f"<b>{'Обычный аккаунт' if acc_type == 'обычный' else 'Аккаунт с отлегой'}</b>\n\n"
        f"📍 Страна: {account['country_flag']} {account['country_name']}\n"
        f"💵 Цена: {format_price(account['price'])}"
    )
    
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("💰 Купить", callback_data=f"purchase_{account.doc_id}"))
    keyboard.add(InlineKeyboardButton("◀️ Назад", callback_data=f"buy_{acc_type}"))
    
    await call.message.delete()
    await bot.send_message(call.from_user.id, text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    await call.answer()

@dp.callback_query_handler(lambda call: call.data.startswith("year_"))
async def year_select(call: CallbackQuery):
    parts = call.data.split("_")
    country_code = parts[1]
    year = parts[2]
    
    account = get_available_account("отлега", country_code, year)
    
    if not account:
        await call.answer("❌ Нет доступных аккаунтов!", show_alert=True)
        return
    
    text = (
        f"<b>Аккаунт с отлегой {year} года</b>\n\n"
        f"📍 Страна: {account['country_flag']} {account['country_name']}\n"
        f"📅 Год регистрации: {year}\n"
        f"💵 Цена: {format_price(account['price'])}"
    )
    
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("💰 Купить", callback_data=f"purchase_{account.doc_id}"))
    keyboard.add(InlineKeyboardButton("◀️ Назад", callback_data=f"aged_country_{country_code}"))
    
    await call.message.delete()
    await bot.send_message(call.from_user.id, text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    await call.answer()

@dp.callback_query_handler(lambda call: call.data.startswith("purchase_"))
async def purchase_account(call: CallbackQuery):
    account_id = int(call.data.replace("purchase_", ""))
    account = accounts_table.get(doc_id=account_id)
    user = get_user(call.from_user.id)
    
    if not account:
        await call.answer("❌ Аккаунт не найден!", show_alert=True)
        return
    
    if user["balance"] < account["price"]:
        await call.answer("❌ Недостаточно средств на балансе!", show_alert=True)
        return
    
    new_balance = user["balance"] - account["price"]
    users_table.update(
        {"balance": new_balance, "purchases": user["purchases"] + 1},
        Query().user_id == call.from_user.id
    )
    
    accounts_table.update(
        {"status": "продан", "buyer_id": call.from_user.id, "sold_at": datetime.now().isoformat()},
        Query().doc_id == account_id
    )
    
    orders_table.insert({
        "phone": account["phone"],
        "buyer_id": call.from_user.id,
        "status": "ожидает_код",
        "created_at": datetime.now().isoformat()
    })
    
    await call.message.delete()
    await bot.send_message(
        call.from_user.id,
        f"✅ <b>Покупка успешна!</b>\n\n"
        f"📱 Номер: <code>{account['phone']}</code>\n\n"
        f"Войдите в аккаунт. Код придет автоматически.",
        parse_mode=ParseMode.HTML
    )
    
    for admin_id in ADMIN_IDS:
        await bot.send_message(
            admin_id,
            f"💰 Продан {account['phone']}\n"
            f"Покупатель: @{call.from_user.username} ({call.from_user.id})\n"
            f"Сумма: {format_price(account['price'])}"
        )
    
    await call.answer()

# ==================== АДМИН-ПАНЕЛЬ ====================
@dp.callback_query_handler(text="admin_panel")
async def admin_panel(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("❌ Нет доступа!", show_alert=True)
        return
    
    await call.message.delete()
    await bot.send_message(
        call.from_user.id,
        "<b>🛠 Админ-панель</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_keyboard()
    )
    await call.answer()

@dp.callback_query_handler(text="admin_add")
async def admin_add_start(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    
    await call.message.delete()
    await bot.send_message(
        call.from_user.id,
        "<b>➕ Добавление аккаунта</b>\n\nВведите название страны (например: США):",
        parse_mode=ParseMode.HTML
    )
    await AddAccount.waiting_country.set()
    await call.answer()

@dp.message_handler(state=AddAccount.waiting_country)
async def admin_add_country(message: Message, state: FSMContext):
    await state.update_data(country=message.text)
    
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("Обычный", callback_data="type_обычный"),
        InlineKeyboardButton("Отлега", callback_data="type_отлега")
    )
    
    await message.answer("Выберите тип аккаунта:", reply_markup=keyboard)
    await AddAccount.waiting_type.set()

@dp.callback_query_handler(lambda call: call.data.startswith("type_"), state=AddAccount.waiting_type)
async def admin_add_type(call: CallbackQuery, state: FSMContext):
    acc_type = call.data.replace("type_", "")
    await state.update_data(acc_type=acc_type)
    
    if acc_type == "отлега":
        await call.message.delete()
        await bot.send_message(call.from_user.id, "Введите год регистрации (например: 2022):")
        await AddAccount.waiting_year.set()
    else:
        await call.message.delete()
        await bot.send_message(call.from_user.id, "Введите цену в рублях (например: 40.30):")
        await AddAccount.waiting_price.set()
    
    await call.answer()

@dp.message_handler(state=AddAccount.waiting_year)
async def admin_add_year(message: Message, state: FSMContext):
    try:
        year = int(message.text)
        await state.update_data(year=year)
        await message.answer("Введите цену в рублях (например: 8640.00):")
        await AddAccount.waiting_price.set()
    except:
        await message.answer("❌ Введите год цифрами!")

@dp.message_handler(state=AddAccount.waiting_price)
async def admin_add_price(message: Message, state: FSMContext):
    try:
        price = float(message.text.replace(",", "."))
        await state.update_data(price=price)
        await message.answer("Введите номер телефона (например: +1234567890):")
        await AddAccount.waiting_phone.set()
    except:
        await message.answer("❌ Введите цену цифрами!")

@dp.message_handler(state=AddAccount.waiting_phone)
async def admin_add_phone(message: Message, state: FSMContext):
    data = await state.get_data()
    phone = message.text
    
    country_flags = {
        "США": "🇺🇸", "Россия": "🇷🇺", "Украина": "🇺🇦",
        "Казахстан": "🇰🇿", "Беларусь": "🇧🇾", "Германия": "🇩🇪",
        "Франция": "🇫🇷", "Англия": "🇬🇧", "Нидерланды": "🇳🇱",
        "Польша": "🇵🇱", "Канада": "🇨🇦", "Испания": "🇪🇸",
        "Италия": "🇮🇹", "Турция": "🇹🇷", "Индия": "🇮🇳",
        "Бразилия": "🇧🇷", "Мексика": "🇲🇽", "Аргентина": "🇦🇷",
        "Швеция": "🇸🇪", "Норвегия": "🇳🇴"
    }
    
    country_name = data["country"]
    flag = country_flags.get(country_name, "🏳️")
    
    account_data = {
        "country_name": country_name,
        "country_flag": flag,
        "country_code": country_name,
        "acc_type": data["acc_type"],
        "price": data["price"],
        "phone": phone,
        "status": "ожидает",
        "has_session": False,
        "added_at": datetime.now().isoformat(),
        "year": data.get("year")
    }
    
    acc_id = accounts_table.insert(account_data)
    
    client = await create_userbot_for_account(phone, acc_id)
    
    if client:
        await message.answer(
            f"📱 Номер: {phone}\n"
            f"🔐 <b>Введи код подтверждения для этого номера:</b>",
            parse_mode=ParseMode.HTML
        )
        await state.update_data(phone=phone, acc_id=acc_id)
        await AddAccount.waiting_code.set()
    else:
        await message.answer("❌ Ошибка создания юзербота")
        await state.finish()

@dp.message_handler(state=AddAccount.waiting_code)
async def admin_add_code(message: Message, state: FSMContext):
    data = await state.get_data()
    phone = data["phone"]
    code = message.text
    
    success = await login_account_userbot(phone, code)
    
    if success:
        await message.answer(
            f"✅ Аккаунт {phone} готов к продаже!\n"
            f"🏳️ Страна: {data.get('country_flag', '🏳️')} {data.get('country', '')}\n"
            f"📦 Тип: {data.get('acc_type', '')}\n"
            f"💵 Цена: {format_price(data.get('price', 0))}"
        )
    else:
        await message.answer(f"❌ Неверный код! Попробуй ещё раз.")
        return
    
    await state.finish()

@dp.callback_query_handler(text="admin_stock")
async def admin_stock(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    
    accounts = accounts_table.search(Query().status == "ожидает")
    text = "<b>📊 Остатки аккаунтов:</b>\n\n"
    
    if not accounts:
        text += "Нет доступных аккаунтов."
    else:
        for country in set(a["country_name"] for a in accounts):
            flag_data = accounts_table.get(Query().country_name == country)
            flag = flag_data["country_flag"] if flag_data else "🏳️"
            regular = len(accounts_table.search(
                (Query().country_name == country) & (Query().acc_type == "обычный") & (Query().status == "ожидает")
            ))
            aged = len(accounts_table.search(
                (Query().country_name == country) & (Query().acc_type == "отлега") & (Query().status == "ожидает")
            ))
            text += f"{flag} {country}: {regular} обычных, {aged} с отлегой\n"
    
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("◀️ Назад", callback_data="admin_panel"))
    
    await call.message.delete()
    await bot.send_message(call.from_user.id, text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    await call.answer()

@dp.callback_query_handler(text="admin_broadcast")
async def admin_broadcast(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    
    await call.message.delete()
    await bot.send_message(call.from_user.id, "Введите текст для рассылки (можно с фото/видео):")
    await Broadcast.waiting_text.set()
    await call.answer()

@dp.message_handler(state=Broadcast.waiting_text, content_types=types.ContentTypes.ANY)
async def broadcast_send(message: Message, state: FSMContext):
    users = users_table.all()
    success = 0
    failed = 0
    
    for user in users:
        try:
            await message.copy_to(user["user_id"])
            success += 1
        except:
            failed += 1
        await asyncio.sleep(0.05)
    
    await message.answer(f"✅ Рассылка завершена!\nУспешно: {success}\nНеудачно: {failed}")
    await state.finish()

@dp.callback_query_handler(text="admin_topup_user")
async def admin_topup_user(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    
    await call.message.delete()
    await bot.send_message(
        call.from_user.id,
        "Введите ID пользователя и сумму через пробел:\n"
        "<code>123456789 100</code>",
        parse_mode=ParseMode.HTML
    )
    
    @dp.message_handler(lambda msg: is_admin(msg.from_user.id) and len(msg.text.split()) == 2)
    async def process_topup(msg: Message):
        try:
            parts = msg.text.split()
            user_id = int(parts[0])
            amount = float(parts[1])
            
            user = get_user(user_id)
            if user:
                new_balance = user["balance"] + amount
                users_table.update({"balance": new_balance}, Query().user_id == user_id)
                await msg.answer(f"✅ Баланс пользователя {user_id} пополнен на {format_price(amount)}")
            else:
                await msg.answer("❌ Пользователь не найден!")
        except:
            await msg.answer("❌ Неверный формат!")
    
    await call.answer()

# ==================== ЗАПУСК ====================
async def on_startup(dp):
    logging.info("Запуск бота...")
    success = await start_main_userbot()
    if success:
        logging.info("Юзербот авторизован")
    else:
        logging.info("Ожидание кода для юзербота...")
    logging.info("Бот запущен!")

if __name__ == '__main__':
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)