import asyncio
import logging
import os
import sys
import random
import threading
from datetime import datetime
from typing import Optional

import aiosqlite
from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    CallbackQuery,
    Message,
    FSInputFile
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from dotenv import load_dotenv
from flask import Flask, jsonify

# Load environment variables
load_dotenv()

# --- Configuration ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")

# --- Flask app for Render ---
app = Flask(__name__)

@app.route('/')
def health():
    return jsonify({"status": "Bot is running"}), 200

# --- Database Setup ---
DB_NAME = "shop_bot.db"

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        # Settings
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        defaults = {
            "welcome_message": "Welcome to our Premium Shop, {name}!",
            "currency": "$",
            "support_link": "https://t.me/telegram",
            "rules": "No spamming. Be respectful.",
            "channel_force_join": "",
            "captcha_enabled": "1",
            "shop_enabled": "1",
            "referral_reward": "5.0",
            "referral_type": "fixed",
            "daily_reward": "2.0",
            "daily_enabled": "1",
            "scratch_enabled": "1",
            "scratch_rewards": "1.0,5.0,10.0",
            "backup_link": "coming soon"
        }
        for key, val in defaults.items():
            await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, val))

        # Users
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                balance REAL DEFAULT 0.0,
                total_spent REAL DEFAULT 0.0,
                referrer_id INTEGER,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                banned INTEGER DEFAULT 0,
                last_daily_claim TIMESTAMP,
                last_scratch_claim TIMESTAMP
            )
        """)

        # Transactions
        await db.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL,
                type TEXT,
                description TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Categories
        await db.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE
            )
        """)

        # Products
        await db.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id INTEGER,
                type TEXT,
                name TEXT,
                description TEXT,
                price REAL,
                content TEXT,
                stock INTEGER DEFAULT -1,
                FOREIGN KEY(category_id) REFERENCES categories(id)
            )
        """)

        # Orders
        await db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                product_id INTEGER,
                status TEXT,
                data TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Tasks
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                description TEXT,
                link TEXT,
                reward REAL
            )
        """)

        # Completed Tasks
        await db.execute("""
            CREATE TABLE IF NOT EXISTS completed_tasks (
                user_id INTEGER,
                task_id INTEGER,
                PRIMARY KEY (user_id, task_id)
            )
        """)

        # Promos
        await db.execute("""
            CREATE TABLE IF NOT EXISTS promos (
                code TEXT PRIMARY KEY,
                reward REAL,
                max_usage INTEGER,
                used_count INTEGER DEFAULT 0,
                expiry_date TEXT
            )
        """)

        # Promo Usage
        await db.execute("""
            CREATE TABLE IF NOT EXISTS promo_usage (
                user_id INTEGER,
                code TEXT,
                PRIMARY KEY (user_id, code)
            )
        """)

        await db.commit()

# --- Database Helpers ---
async def get_setting(key):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

async def update_setting(key, value):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
        await db.commit()

async def get_user(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            return await cursor.fetchone()

async def add_transaction(user_id, amount, ttype, description):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            INSERT INTO transactions (user_id, amount, type, description)
            VALUES (?, ?, ?, ?)
        """, (user_id, amount, ttype, description))
        await db.commit()

async def add_user(user_id, username, full_name, referrer_id=None):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,)) as cursor:
            if await cursor.fetchone():
                return

        await db.execute("""
            INSERT INTO users (user_id, username, full_name, referrer_id)
            VALUES (?, ?, ?, ?)
        """, (user_id, username, full_name, referrer_id))

        if referrer_id:
            reward_type = await get_setting("referral_type")
            if reward_type == "fixed":
                reward = float(await get_setting("referral_reward") or 0)
                await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (reward, referrer_id))
                await add_transaction(referrer_id, reward, "credit", f"Referral bonus for new user {user_id}")

                # Send notification
                bot = Bot(token=TOKEN)
                try:
                    await bot.send_message(referrer_id, f"🎉 Darling, someone joined using your link! You got {reward} {await get_setting('currency')}!")
                except:
                    pass
                finally:
                    await bot.session.close()

        await db.commit()

# --- Force Join Check ---
async def is_user_member(bot: Bot, user_id: int, channel: str) -> bool:
    if not channel:
        return True
    try:
        chat = await bot.get_chat(channel)
        member = await bot.get_chat_member(chat.id, user_id)
        return member.status not in ["left", "kicked"]
    except:
        return False

def force_join_check(handler):
    async def wrapper(message: Message, *args, **kwargs):
        bot = message.bot
        user_id = message.from_user.id
        channel = await get_setting("channel_force_join")
        if channel and not await is_user_member(bot, user_id, channel):
            builder = InlineKeyboardBuilder()
            builder.button(text="✅ I've Joined", callback_data="check_join")
            await message.answer(
                f"⚠️ You must join our channel first: {channel}",
                reply_markup=builder.as_markup()
            )
            return
        return await handler(message, *args, **kwargs)
    return wrapper

# --- FSM States ---
class AdminStates(StatesGroup):
    waiting_for_broadcast = State()
    waiting_for_category_name = State()
    waiting_for_category_edit = State()
    waiting_for_product_category = State()
    waiting_for_product_type = State()
    waiting_for_product_name = State()
    waiting_for_product_desc = State()
    waiting_for_product_price = State()
    waiting_for_product_stock = State()
    waiting_for_product_content = State()
    waiting_for_user_search = State()
    waiting_for_balance_change = State()
    waiting_for_setting_key = State()
    waiting_for_setting_value = State()
    waiting_for_task_desc = State()
    waiting_for_task_link = State()
    waiting_for_task_reward = State()
    waiting_for_promo_code = State()
    waiting_for_promo_reward = State()
    waiting_for_promo_limit = State()
    waiting_for_order_status = State()

class ShopStates(StatesGroup):
    captcha = State()
    waiting_for_custom_order_input = State()

class UserStates(StatesGroup):
    waiting_for_promo = State()
    waiting_for_daily_claim = State()
    waiting_for_scratch_claim = State()

# --- Bot & Router ---
router = Router()

# --- Keyboards ---
def main_menu_kb(is_admin=False):
    builder = ReplyKeyboardBuilder()
    builder.button(text="🛍️ Shop")
    builder.button(text="👤 Profile")
    builder.button(text="🎁 Daily Bonus")
    builder.button(text="✨ Scratch Card")
    builder.button(text="📋 Tasks")
    builder.button(text="ℹ️ Support")
    builder.button(text="📜 Rules")
    if is_admin:
        builder.button(text="⚙️ Admin Panel")
    builder.adjust(2)
    return builder.as_markup(resize_keyboard=True)

def admin_panel_kb():
    builder = InlineKeyboardBuilder()
    builder.button(text="📊 Stats", callback_data="admin_stats")
    builder.button(text="👥 Users", callback_data="admin_users")
    builder.button(text="📢 Broadcast", callback_data="admin_broadcast")
    builder.button(text="🛍 Shop Mgmt", callback_data="admin_shop")
    builder.button(text="📦 Orders", callback_data="admin_orders")
    builder.button(text="⚙️ Settings", callback_data="admin_settings")
    builder.button(text="🎁 Promos", callback_data="admin_promos")
    builder.button(text="📋 Tasks Mgmt", callback_data="admin_tasks")
    builder.button(text="🗄️ Backup", callback_data="admin_backup")
    builder.adjust(2)
    return builder.as_markup()

def shop_mgmt_kb():
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Add Category", callback_data="admin_add_cat")
    builder.button(text="✏️ Edit Category", callback_data="admin_edit_cat")
    builder.button(text="🗑️ Delete Category", callback_data="admin_del_cat")
    builder.button(text="➕ Add Product", callback_data="admin_add_prod")
    builder.button(text="✏️ Edit Product", callback_data="admin_edit_prod")
    builder.button(text="🗑️ Delete Product", callback_data="admin_del_prod")
    builder.button(text="🔙 Back", callback_data="admin_panel")
    builder.adjust(2)
    return builder.as_markup()

# --- Emoji Captcha ---
EMOJI_LIST = ["🐟", "🐱", "🐶", "🐼", "🐨", "🦊", "🐸", "🐧", "🐝", "🐞"]

@router.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    args = message.text.split()
    referrer_id = None
    if len(args) > 1 and args[1].isdigit():
        referrer_id_arg = int(args[1])
        if referrer_id_arg != message.from_user.id:
            referrer = await get_user(referrer_id_arg)
            if referrer:
                referrer_id = referrer_id_arg

    await add_user(message.from_user.id, message.from_user.username, message.from_user.full_name, referrer_id)

    channel = await get_setting("channel_force_join")
    if channel and not await is_user_member(message.bot, message.from_user.id, channel):
        builder = InlineKeyboardBuilder()
        builder.button(text="✅ I've Joined", callback_data="check_join")
        await message.answer(
            f"⚠️ You must join our channel first: {channel}",
            reply_markup=builder.as_markup()
        )
        return

    captcha_enabled = await get_setting("captcha_enabled")
    if captcha_enabled == "1":
        emoji = random.choice(EMOJI_LIST)
        await state.update_data(captcha_emoji=emoji)
        await state.set_state(ShopStates.captcha)
        builder = InlineKeyboardBuilder()
        builder.button(text=emoji, callback_data="captcha_click")
        await message.answer(
            "🔒 Security Check: Click the emoji below:",
            reply_markup=builder.as_markup()
        )
    else:
        await show_welcome(message)

@router.callback_query(F.data == "captcha_click", ShopStates.captcha)
async def process_captcha(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.delete()
    await show_welcome(callback.message)

@router.callback_query(F.data == "check_join")
async def check_join_callback(callback: CallbackQuery):
    channel = await get_setting("channel_force_join")
    if await is_user_member(callback.bot, callback.from_user.id, channel):
        await callback.message.delete()
        await show_welcome(callback.message)
    else:
        await callback.answer("You haven't joined yet!", show_alert=True)

async def show_welcome(message: types.Message):
    welcome_text = await get_setting("welcome_message")
    user = await get_user(message.from_user.id)
    is_admin = str(message.from_user.id) == str(ADMIN_ID)

    if user and user['banned']:
        await message.answer("🚫 You are banned from this bot.")
        return

    welcome_text = welcome_text.replace("{name}", message.from_user.full_name)
    await message.answer(
        welcome_text,
        reply_markup=main_menu_kb(is_admin),
        parse_mode=ParseMode.MARKDOWN
    )

# --- Profile & Transactions ---
@router.message(F.text == "👤 Profile")
@force_join_check
async def profile(message: types.Message):
    user = await get_user(message.from_user.id)
    currency = await get_setting("currency")

    if not user:
        await message.answer("User not found. Try /start")
        return

    total_spent = user['total_spent']
    if total_spent < 100:
        level = "🥉 Bronze"
    elif total_spent < 500:
        level = "🥈 Silver"
    else:
        level = "🥇 Gold"

    text = (
        f"👤 *Your Profile*\n\n"
        f"🆔 ID: `{user['user_id']}`\n"
        f"💰 Balance: `{user['balance']:.2f} {currency}`\n"
        f"💸 Total Spent: `{total_spent:.2f} {currency}`\n"
        f"🏅 Level: {level}\n"
        f"📅 Joined: {user['joined_at'].split(' ')[0]}\n"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="📦 My Orders", callback_data="my_orders")
    builder.button(text="📜 Transaction History", callback_data="my_transactions")
    builder.button(text="🎁 Redeem Promo", callback_data="redeem_promo")
    builder.adjust(1)

    bot_info = await message.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={user['user_id']}"

    await message.answer(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.MARKDOWN)
    await message.answer(f"🔗 *Referral Link:*\n`{ref_link}`\nShare this link to earn rewards!", parse_mode=ParseMode.MARKDOWN)

@router.callback_query(F.data == "my_transactions")
async def my_transactions(callback: CallbackQuery):
    user_id = callback.from_user.id
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT * FROM transactions WHERE user_id = ?
            ORDER BY timestamp DESC LIMIT 10
        """, (user_id,)) as cursor:
            txs = await cursor.fetchall()

    if not txs:
        await callback.message.answer("No transactions found.")
        return

    text = "📜 *Last 10 Transactions:*\n\n"
    for tx in txs:
        sign = "+" if tx['type'] == 'credit' else "-"
        text += f"• {sign}{tx['amount']:.2f} - {tx['description']}\n  `{tx['timestamp']}`\n"

    await callback.message.answer(text, parse_mode=ParseMode.MARKDOWN)

# --- Shop ---
@router.message(F.text == "🛍️ Shop")
@force_join_check
async def shop_entry(message: types.Message):
    shop_enabled = await get_setting("shop_enabled")
    if shop_enabled == "0":
        await message.answer("⚠️ Shop is currently disabled.")
        return

    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM categories") as cursor:
            categories = await cursor.fetchall()

    if not categories:
        await message.answer("No categories available.")
        return

    builder = InlineKeyboardBuilder()
    for cat in categories:
        builder.button(text=cat['name'], callback_data=f"cat_{cat['id']}")
    builder.adjust(2)

    await message.answer("📂 Select a Category:", reply_markup=builder.as_markup())

@router.callback_query(F.data.startswith("cat_"))
async def show_products(callback: CallbackQuery):
    cat_id = int(callback.data.split("_")[1])
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM products WHERE category_id = ?", (cat_id,)) as cursor:
            products = await cursor.fetchall()

    if not products:
        await callback.message.answer("No products in this category.")
        return

    builder = InlineKeyboardBuilder()
    for prod in products:
        stock_text = "∞" if prod['stock'] == -1 else prod['stock']
        builder.button(text=f"{prod['name']} | {prod['price']:.2f} | Stock: {stock_text}", callback_data=f"prod_{prod['id']}")
    builder.adjust(1)

    await callback.message.answer("📦 Available Products:", reply_markup=builder.as_markup())

@router.callback_query(F.data.startswith("prod_"))
async def show_product_details(callback: CallbackQuery):
    prod_id = int(callback.data.split("_")[1])
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM products WHERE id = ?", (prod_id,)) as cursor:
            prod = await cursor.fetchone()

    if not prod:
        await callback.message.answer("Product not found.")
        return

    currency = await get_setting("currency")
    text = (
        f"📦 *{prod['name']}*\n\n"
        f"📝 {prod['description']}\n\n"
        f"💰 Price: `{prod['price']:.2f} {currency}`\n"
        f"📦 Stock: `{'Unlimited' if prod['stock'] == -1 else prod['stock']}`\n"
        f"Type: `{prod['type']}`"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="💳 Buy Now", callback_data=f"buy_{prod['id']}")
    builder.button(text="🔙 Back", callback_data="shop_main")

    await callback.message.answer(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.MARKDOWN)

@router.callback_query(F.data.startswith("buy_"))
async def buy_product(callback: CallbackQuery):
    prod_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id

    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM products WHERE id = ?", (prod_id,)) as cursor:
            prod = await cursor.fetchone()

        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            user = await cursor.fetchone()

    if not prod:
        await callback.answer("Product not found.")
        return

    if prod['stock'] != -1 and prod['stock'] <= 0:
        await callback.answer("Out of stock!")
        return

    if user['balance'] < prod['price']:
        await callback.answer("Insufficient balance!")
        return

    new_balance = user['balance'] - prod['price']
    new_stock = prod['stock'] - 1 if prod['stock'] != -1 else -1

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET balance = ?, total_spent = total_spent + ? WHERE user_id = ?",
                         (new_balance, prod['price'], user_id))
        if new_stock != -1:
            await db.execute("UPDATE products SET stock = ? WHERE id = ?", (new_stock, prod_id))

        status = "delivered" if prod['type'] in ['digital', 'file'] else "pending"
        order_data = prod['content'] if status == "delivered" and prod['type'] != 'physical' else "Waiting for manual processing"
        await db.execute("INSERT INTO orders (user_id, product_id, status, data) VALUES (?, ?, ?, ?)",
                         (user_id, prod_id, status, order_data))
        await db.commit()

    await add_transaction(user_id, -prod['price'], "debit", f"Purchase: {prod['name']}")

    await callback.message.answer(f"✅ Purchase Successful!\nNew Balance: {new_balance:.2f} {await get_setting('currency')}")

    if prod['type'] == 'file':
        try:
            await callback.message.answer_document(FSInputFile(prod['content'], filename=f"{prod['name']}.dat"))
        except Exception:
            await callback.message.answer(f"📄 Here is your content/file ID: `{prod['content']}`", parse_mode=ParseMode.MARKDOWN)
    elif prod['type'] == 'digital':
        await callback.message.answer(f"📦 Your Item:\n`{prod['content']}`", parse_mode=ParseMode.MARKDOWN)
    elif prod['type'] == 'physical':
        await callback.message.answer("📦 Order placed! An admin will review it shortly.")

@router.callback_query(F.data == "my_orders")
async def my_orders(callback: CallbackQuery):
    user_id = callback.from_user.id
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT orders.*, products.name FROM orders
            JOIN products ON orders.product_id = products.id
            WHERE user_id = ?
            ORDER BY created_at DESC LIMIT 10
        """, (user_id,)) as cursor:
            orders = await cursor.fetchall()

    if not orders:
        await callback.message.answer("No orders found.")
        return

    text = "📦 *Recent Orders:*\n\n"
    for order in orders:
        text += f"🔹 {order['name']} - Status: `{order['status'].upper()}`\n"

    await callback.message.answer(text, parse_mode=ParseMode.MARKDOWN)

# --- Promo Code System ---
@router.callback_query(F.data == "redeem_promo")
async def redeem_promo_ask(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("🎁 Enter your Promo Code:")
    await state.set_state(UserStates.waiting_for_promo)

@router.message(UserStates.waiting_for_promo)
async def redeem_promo_process(message: types.Message, state: FSMContext):
    code = message.text.strip()
    user_id = message.from_user.id

    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM promos WHERE code = ?", (code,)) as cursor:
            promo = await cursor.fetchone()

        if not promo:
            await message.answer("❌ Invalid code.")
            await state.clear()
            return

        if promo['max_usage'] != -1 and promo['used_count'] >= promo['max_usage']:
            await message.answer("❌ Code limit reached.")
            await state.clear()
            return

        async with db.execute("SELECT * FROM promo_usage WHERE user_id = ? AND code = ?", (user_id, code)) as cursor:
            if await cursor.fetchone():
                await message.answer("❌ You already used this code.")
                await state.clear()
                return

        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (promo['reward'], user_id))
        await db.execute("UPDATE promos SET used_count = used_count + 1 WHERE code = ?", (code,))
        await db.execute("INSERT INTO promo_usage (user_id, code) VALUES (?, ?)", (user_id, code))
        await db.commit()

    await add_transaction(user_id, promo['reward'], "credit", f"Promo code: {code}")
    await message.answer(f"✅ Code Redeemed! Added {promo['reward']:.2f} to your balance.")
    await state.clear()

# --- Task System ---
@router.message(F.text == "📋 Tasks")
@force_join_check
async def tasks_list(message: types.Message):
    user_id = message.from_user.id
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT * FROM tasks
            WHERE id NOT IN (SELECT task_id FROM completed_tasks WHERE user_id = ?)
        """, (user_id,)) as cursor:
            tasks = await cursor.fetchall()

    if not tasks:
        await message.answer("✅ No new tasks available.")
        return

    text = "📋 *Available Tasks:*\n\n"
    builder = InlineKeyboardBuilder()

    for task in tasks:
        text += f"🔹 {task['description']} - Reward: {task['reward']:.2f}\n"
        builder.button(text=f"✅ Complete: {task['description'][:15]}...", callback_data=f"do_task_{task['id']}")

    builder.adjust(1)
    await message.answer(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.MARKDOWN)

@router.callback_query(F.data.startswith("do_task_"))
async def do_task(callback: CallbackQuery):
    task_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id

    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)) as cursor:
            task = await cursor.fetchone()

        if not task:
            await callback.answer("Task not found.")
            return

        async with db.execute("SELECT * FROM completed_tasks WHERE user_id = ? AND task_id = ?", (user_id, task_id)) as cursor:
            if await cursor.fetchone():
                await callback.answer("Already completed!")
                return

        await db.execute("INSERT INTO completed_tasks (user_id, task_id) VALUES (?, ?)", (user_id, task_id))
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (task['reward'], user_id))
        await db.commit()

    await add_transaction(user_id, task['reward'], "credit", f"Task: {task['description'][:30]}")
    await callback.message.answer(f"✅ Task Completed! Reward: {task['reward']:.2f}")

# --- Daily Bonus System ---
@router.message(F.text == "🎁 Daily Bonus")
@force_join_check
async def daily_bonus_entry(message: types.Message, state: FSMContext):
    daily_enabled = await get_setting("daily_enabled")
    if daily_enabled != "1":
        await message.answer("The Daily Bonus feature is currently disabled by admin.")
        return
    await state.set_state(UserStates.waiting_for_daily_claim)
    await process_daily_claim(message, state)

@router.message(UserStates.waiting_for_daily_claim)
async def process_daily_claim(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    now = datetime.now()

    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        user = await get_user(user_id)
        if not user:
            await message.answer("User not found. Please use /start.")
            return

        last_claim = user['last_daily_claim']
        reward = float(await get_setting("daily_reward") or 0.0)

        if last_claim:
            last_claim_dt = datetime.strptime(last_claim, '%Y-%m-%d %H:%M:%S.%f')
            if (now - last_claim_dt).total_seconds() < 86400:
                await message.answer("You have already claimed today's bonus. Try again tomorrow!")
                return

        await db.execute("UPDATE users SET balance = balance + ?, last_daily_claim = ? WHERE user_id = ?",
                         (reward, now.strftime('%Y-%m-%d %H:%M:%S.%f'), user_id))
        await db.commit()

    await add_transaction(user_id, reward, "credit", "Daily bonus")
    await message.answer(f"🎉 You claimed your Daily Bonus! Added: {reward:.2f}")

# --- Scratch Card System ---
@router.message(F.text == "✨ Scratch Card")
@force_join_check
async def scratch_card_entry(message: types.Message, state: FSMContext):
    scratch_enabled = await get_setting("scratch_enabled")
    if scratch_enabled != "1":
        await message.answer("The Scratch Card feature is currently disabled by admin.")
        return
    await state.set_state(UserStates.waiting_for_scratch_claim)
    await process_scratch_claim(message, state)

@router.message(UserStates.waiting_for_scratch_claim)
async def process_scratch_claim(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    now = datetime.now()

    async with aiosqlite.connect(DB_NAME) as db:
        user = await get_user(user_id)
        if not user:
            await message.answer("User not found. Please use /start.")
            return

        last_claim = user['last_scratch_claim']

        if last_claim:
            last_claim_dt = datetime.strptime(last_claim, '%Y-%m-%d %H:%M:%S.%f')
            if (now - last_claim_dt).total_seconds() < 86400:
                await message.answer("You have already scratched today. Try again tomorrow!")
                return

        rewards_str = await get_setting("scratch_rewards")
        if not rewards_str:
            await message.answer("Scratch rewards are not set up.")
            return

        rewards = [float(r.strip()) for r in rewards_str.split(',') if r.strip()]
        if not rewards:
            await message.answer("Scratch rewards list is empty.")
            return

        reward = random.choice(rewards)

        await db.execute("UPDATE users SET balance = balance + ?, last_scratch_claim = ? WHERE user_id = ?",
                         (reward, now.strftime('%Y-%m-%d %H:%M:%S.%f'), user_id))
        await db.commit()

    await add_transaction(user_id, reward, "credit", "Scratch card win")
    await message.answer(f"✨ You scratched and won: {reward:.2f}!\nAdded to your balance.",
                         reply_markup=main_menu_kb(str(user_id) == str(ADMIN_ID)))

# --- Support & Rules ---
@router.message(F.text == "ℹ️ Support")
@force_join_check
async def support(message: types.Message):
    link = await get_setting("support_link")
    await message.answer(f"ℹ️ Contact Support: {link}")

@router.message(F.text == "📜 Rules")
@force_join_check
async def rules(message: types.Message):
    rules_text = await get_setting("rules")
    await message.answer(f"📜 *Rules*:\n\n{rules_text}", parse_mode=ParseMode.MARKDOWN)

# --- Admin Handlers ---
@router.message(F.text == "⚙️ Admin Panel")
async def admin_panel_entry(message: types.Message):
    if str(message.from_user.id) != str(ADMIN_ID):
        return
    await message.answer("🔧 Admin Control Center", reply_markup=admin_panel_kb())

@router.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cursor:
            total_users = (await cursor.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM orders WHERE status != 'pending'") as cursor:
            total_completed_orders = (await cursor.fetchone())[0]
        async with db.execute("SELECT SUM(price) FROM products JOIN orders ON products.id = orders.product_id WHERE orders.status != 'pending'") as cursor:
            revenue = (await cursor.fetchone())[0] or 0.0
        async with db.execute("SELECT SUM(balance) FROM users") as cursor:
            total_balance = (await cursor.fetchone())[0] or 0.0

    text = (
        "📊 *Live Statistics*\n\n"
        f"👥 Total Users: `{total_users}`\n"
        f"✅ Completed Orders: `{total_completed_orders}`\n"
        f"💰 Total Revenue: `{revenue:.2f}`\n"
        f"💎 Total User Balance: `{total_balance:.2f}`\n"
    )
    await callback.message.edit_text(text, reply_markup=admin_panel_kb(), parse_mode=ParseMode.MARKDOWN)

@router.callback_query(F.data == "admin_backup")
async def admin_backup_info(callback: CallbackQuery):
    backup_link = await get_setting("backup_link")
    text = (
        "🗄️ *Database Backup*\n\n"
        "The bot automatically saves data in `shop_bot.db`.\n\n"
        f"Backup link (placeholder): `{backup_link}`\n\n"
        "To enable cloud backup, implement external logic (e.g., upload DB to Google Drive)."
    )
    await callback.message.edit_text(text, reply_markup=admin_panel_kb(), parse_mode=ParseMode.MARKDOWN)

@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_ask(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("📝 Send the message you want to broadcast (Text, Photo, or Video):")
    await state.set_state(AdminStates.waiting_for_broadcast)

@router.message(AdminStates.waiting_for_broadcast)
async def admin_broadcast_send(message: types.Message, state: FSMContext, bot: Bot):
    await state.clear()
    msg = await message.answer("⏳ Starting broadcast...")

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM users") as cursor:
            users = await cursor.fetchall()

    count = 0
    fail_count = 0
    for user in users:
        try:
            await message.copy_to(user[0])
            count += 1
            if count % 20 == 0:
                await asyncio.sleep(1)
        except Exception:
            fail_count += 1

    await msg.edit_text(f"✅ Broadcast finished. Sent successfully to {count} users. Failed for {fail_count} users.")

@router.callback_query(F.data == "admin_shop")
async def admin_shop_menu(callback: CallbackQuery):
    await callback.message.edit_text("🛍 Shop Management", reply_markup=shop_mgmt_kb())

# --- Category Management ---
@router.callback_query(F.data == "admin_add_cat")
async def admin_add_cat(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("📝 Enter new Category Name:")
    await state.set_state(AdminStates.waiting_for_category_name)

@router.message(AdminStates.waiting_for_category_name)
async def admin_save_cat(message: types.Message, state: FSMContext):
    name = message.text
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (name,))
        await db.commit()
    await message.answer(f"✅ Category '{name}' added.")
    await state.clear()

@router.callback_query(F.data == "admin_edit_cat")
async def admin_edit_cat_list(callback: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM categories") as cursor:
            cats = await cursor.fetchall()
    if not cats:
        await callback.message.answer("No categories to edit.")
        return
    builder = InlineKeyboardBuilder()
    for cat in cats:
        builder.button(text=cat['name'], callback_data=f"edit_cat_{cat['id']}")
    builder.adjust(2)
    await callback.message.answer("Select category to edit:", reply_markup=builder.as_markup())

@router.callback_query(F.data.startswith("edit_cat_"))
async def admin_edit_cat_prompt(callback: CallbackQuery, state: FSMContext):
    cat_id = int(callback.data.split("_")[2])
    await state.update_data(edit_cat_id=cat_id)
    await callback.message.answer("Enter new name for the category:")
    await state.set_state(AdminStates.waiting_for_category_edit)

@router.message(AdminStates.waiting_for_category_edit)
async def admin_update_cat(message: types.Message, state: FSMContext):
    data = await state.get_data()
    cat_id = data['edit_cat_id']
    new_name = message.text
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE categories SET name = ? WHERE id = ?", (new_name, cat_id))
        await db.commit()
    await message.answer(f"✅ Category updated to '{new_name}'.")
    await state.clear()

@router.callback_query(F.data == "admin_del_cat")
async def admin_del_cat_list(callback: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM categories") as cursor:
            cats = await cursor.fetchall()
    if not cats:
        await callback.message.answer("No categories to delete.")
        return
    builder = InlineKeyboardBuilder()
    for cat in cats:
        builder.button(text=f"❌ {cat['name']}", callback_data=f"del_cat_{cat['id']}")
    builder.adjust(2)
    await callback.message.answer("Select category to delete:", reply_markup=builder.as_markup())

@router.callback_query(F.data.startswith("del_cat_"))
async def admin_delete_cat(callback: CallbackQuery):
    cat_id = int(callback.data.split("_")[2])
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
        await db.commit()
    await callback.answer("Category deleted.")
    await callback.message.delete()

# --- Product Management ---
@router.callback_query(F.data == "admin_add_prod")
async def admin_add_prod_start(callback: CallbackQuery, state: FSMContext):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM categories") as cursor:
            categories = await cursor.fetchall()

    if not categories:
        await callback.message.answer("Please add a category first using '➕ Add Category'.")
        return

    builder = ReplyKeyboardBuilder()
    for cat in categories:
        builder.button(text=cat['name'])
    builder.adjust(2)

    await callback.message.answer("📂 Select Category for new product:", reply_markup=builder.as_markup(one_time_keyboard=True, resize_keyboard=True))
    await state.set_state(AdminStates.waiting_for_product_category)

@router.message(AdminStates.waiting_for_product_category)
async def admin_prod_cat_selected(message: types.Message, state: FSMContext):
    cat_name = message.text
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT id FROM categories WHERE name = ?", (cat_name,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                await message.answer("Invalid Category. Please select from the keyboard.")
                return
            cat_id = row[0]

    await state.update_data(cat_id=cat_id)
    await message.answer("📝 Product Name:", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(AdminStates.waiting_for_product_name)

@router.message(AdminStates.waiting_for_product_name)
async def admin_prod_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("📝 Description:")
    await state.set_state(AdminStates.waiting_for_product_desc)

@router.message(AdminStates.waiting_for_product_desc)
async def admin_prod_desc(message: types.Message, state: FSMContext):
    await state.update_data(desc=message.text)
    await message.answer("💰 Price:")
    await state.set_state(AdminStates.waiting_for_product_price)

@router.message(AdminStates.waiting_for_product_price)
async def admin_prod_price(message: types.Message, state: FSMContext):
    try:
        price = float(message.text)
        await state.update_data(price=price)

        builder = ReplyKeyboardBuilder()
        builder.button(text="digital")
        builder.button(text="file")
        builder.button(text="physical")
        builder.adjust(3)

        await message.answer("📦 Product Type:", reply_markup=builder.as_markup(one_time_keyboard=True, resize_keyboard=True))
        await state.set_state(AdminStates.waiting_for_product_type)
    except ValueError:
        await message.answer("Invalid price. Enter a number.")

@router.message(AdminStates.waiting_for_product_type)
async def admin_prod_type(message: types.Message, state: FSMContext):
    ptype = message.text.lower()
    if ptype not in ['digital', 'file', 'physical']:
        await message.answer("Invalid type. Select from the keyboard.")
        return

    await state.update_data(type=ptype)

    if ptype == 'physical':
        await state.update_data(content="Physical Item: Awaiting user data/manual fulfillment.")
        await message.answer("📦 Stock amount (-1 for unlimited):", reply_markup=types.ReplyKeyboardRemove())
        await state.set_state(AdminStates.waiting_for_product_stock)
    else:
        await message.answer("📄 Content (Text for digital, Send the File/Photo for file type, or enter File ID):", reply_markup=types.ReplyKeyboardRemove())
        await state.set_state(AdminStates.waiting_for_product_content)

@router.message(AdminStates.waiting_for_product_content)
async def admin_prod_content(message: types.Message, state: FSMContext):
    content = ""
    if message.document:
        content = message.document.file_id
    elif message.photo:
        content = message.photo[-1].file_id
    elif message.text:
        content = message.text

    if not content:
        await message.answer("Please send a file/photo or paste the content/File ID.")
        return

    await state.update_data(content=content)
    await message.answer("📦 Stock amount (-1 for unlimited):")
    await state.set_state(AdminStates.waiting_for_product_stock)

@router.message(AdminStates.waiting_for_product_stock)
async def admin_prod_stock(message: types.Message, state: FSMContext):
    try:
        stock = int(message.text)
        data = await state.get_data()

        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("""
                INSERT INTO products (category_id, name, description, price, type, content, stock)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (data['cat_id'], data['name'], data['desc'], data['price'], data['type'], data['content'], stock))
            await db.commit()

        await message.answer("✅ Product Added!", reply_markup=admin_panel_kb())
        await state.clear()
    except ValueError:
        await message.answer("Invalid stock. Enter an integer (-1 for unlimited).")

@router.callback_query(F.data == "admin_edit_prod")
async def admin_edit_prod_list(callback: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT products.*, categories.name as cat_name FROM products JOIN categories ON products.category_id = categories.id") as cursor:
            prods = await cursor.fetchall()
    if not prods:
        await callback.message.answer("No products to edit.")
        return
    builder = InlineKeyboardBuilder()
    for prod in prods:
        builder.button(text=f"{prod['name']} ({prod['cat_name']})", callback_data=f"edit_prod_{prod['id']}")
    builder.adjust(1)
    await callback.message.answer("Select product to edit:", reply_markup=builder.as_markup())

# Edit product flow can be added similarly; for brevity, we'll stop here. In a full bot, you'd add editing handlers.

@router.callback_query(F.data == "admin_del_prod")
async def admin_del_prod_list(callback: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT products.*, categories.name as cat_name FROM products JOIN categories ON products.category_id = categories.id") as cursor:
            prods = await cursor.fetchall()
    if not prods:
        await callback.message.answer("No products to delete.")
        return
    builder = InlineKeyboardBuilder()
    for prod in prods:
        builder.button(text=f"❌ {prod['name']} ({prod['cat_name']})", callback_data=f"del_prod_{prod['id']}")
    builder.adjust(1)
    await callback.message.answer("Select product to delete:", reply_markup=builder.as_markup())

@router.callback_query(F.data.startswith("del_prod_"))
async def admin_delete_prod(callback: CallbackQuery):
    prod_id = int(callback.data.split("_")[2])
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM products WHERE id = ?", (prod_id,))
        await db.commit()
    await callback.answer("Product deleted.")
    await callback.message.delete()

# --- Admin Users ---
@router.callback_query(F.data == "admin_users")
async def admin_users_menu(callback: CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.button(text="🔎 Search User", callback_data="admin_search_user")
    builder.adjust(1)
    await callback.message.edit_text("👥 User Management", reply_markup=builder.as_markup())

@router.callback_query(F.data == "admin_search_user")
async def admin_search_user(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("🆔 Enter User ID or Username:")
    await state.set_state(AdminStates.waiting_for_user_search)

@router.message(AdminStates.waiting_for_user_search)
async def admin_show_user(message: types.Message, state: FSMContext):
    query = message.text.strip().replace("@", "")
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        if query.isdigit():
            sql = "SELECT * FROM users WHERE user_id = ?"
            params = (int(query),)
        else:
            sql = "SELECT * FROM users WHERE username = ?"
            params = (query,)

        async with db.execute(sql, params) as cursor:
            user = await cursor.fetchone()

    if not user:
        await message.answer("User not found.")
        await state.clear()
        return

    text = (
        f"👤 *User Details*\n"
        f"ID: `{user['user_id']}`\n"
        f"Name: {user['full_name']}\n"
        f"Username: @{user['username']}\n"
        f"Balance: `{user['balance']:.2f}`\n"
        f"Total Spent: `{user['total_spent']:.2f}`\n"
        f"Banned: *{'YES' if user['banned'] else 'NO'}*"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="💰 Give Balance", callback_data=f"admin_give_bal_{user['user_id']}")
    builder.button(text="🚫 Ban/Unban", callback_data=f"admin_ban_{user['user_id']}")
    builder.button(text="🔙 Back to Users", callback_data="admin_users")

    await message.answer(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.MARKDOWN)
    await state.clear()

@router.callback_query(F.data.startswith("admin_give_bal_"))
async def admin_give_balance_ask(callback: CallbackQuery, state: FSMContext):
    user_id = int(callback.data.split("_")[3])
    await state.update_data(target_user_id=user_id)
    await callback.message.answer("💰 Enter amount to add (negative to subtract):")
    await state.set_state(AdminStates.waiting_for_balance_change)

@router.message(AdminStates.waiting_for_balance_change)
async def admin_give_balance_process(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text)
        data = await state.get_data()
        user_id = data['target_user_id']

        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
            await db.commit()

        ttype = "credit" if amount > 0 else "debit"
        await add_transaction(user_id, amount, ttype, "Admin adjustment")

        await message.answer(f"✅ Balance updated by {amount:.2f} for user {user_id}.", reply_markup=admin_panel_kb())
        await state.clear()
    except ValueError:
        await message.answer("Invalid amount.")

@router.callback_query(F.data.startswith("admin_ban_"))
async def admin_ban_user(callback: CallbackQuery):
    user_id = int(callback.data.split("_")[2])
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT banned FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            current_status = row[0] if row else 0

        new_status = 1 if current_status == 0 else 0
        await db.execute("UPDATE users SET banned = ? WHERE user_id = ?", (new_status, user_id))
        await db.commit()

    status_text = "Banned" if new_status else "Unbanned"
    await callback.answer(f"User {status_text}!")

# --- Admin Orders ---
@router.callback_query(F.data == "admin_orders")
async def admin_orders_list(callback: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT orders.*, products.name, users.username
            FROM orders
            JOIN products ON orders.product_id = products.id
            JOIN users ON orders.user_id = users.user_id
            WHERE orders.status = 'pending'
            ORDER BY created_at DESC
        """) as cursor:
            orders = await cursor.fetchall()

    if not orders:
        await callback.message.answer("No pending orders.")
        return

    builder = InlineKeyboardBuilder()
    for order in orders:
        builder.button(text=f"Order #{order['id']} - {order['name']}", callback_data=f"admin_order_{order['id']}")
    builder.adjust(1)
    await callback.message.answer("📦 Pending Orders:", reply_markup=builder.as_markup())

@router.callback_query(F.data.startswith("admin_order_"))
async def admin_order_detail(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split("_")[2])
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT orders.*, products.name, products.type, users.username
            FROM orders
            JOIN products ON orders.product_id = products.id
            JOIN users ON orders.user_id = users.user_id
            WHERE orders.id = ?
        """, (order_id,)) as cursor:
            order = await cursor.fetchone()

    if not order:
        await callback.answer("Order not found.")
        return

    text = (
        f"📦 *Order #{order['id']}*\n"
        f"User: @{order['username']} (ID: {order['user_id']})\n"
        f"Product: {order['name']}\n"
        f"Type: {order['type']}\n"
        f"Status: {order['status']}\n"
        f"Data: {order['data']}"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Mark Delivered", callback_data=f"admin_order_deliver_{order['id']}")
    builder.button(text="❌ Reject", callback_data=f"admin_order_reject_{order['id']}")
    builder.button(text="🔙 Back", callback_data="admin_orders")

    await callback.message.answer(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.MARKDOWN)

@router.callback_query(F.data.startswith("admin_order_deliver_"))
async def admin_order_deliver(callback: CallbackQuery):
    order_id = int(callback.data.split("_")[3])
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE orders SET status = 'delivered' WHERE id = ?", (order_id,))
        await db.commit()
    await callback.answer("Order marked as delivered.")
    await callback.message.delete()

@router.callback_query(F.data.startswith("admin_order_reject_"))
async def admin_order_reject(callback: CallbackQuery):
    order_id = int(callback.data.split("_")[3])
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE orders SET status = 'rejected' WHERE id = ?", (order_id,))
        await db.commit()
    await callback.answer("Order rejected.")
    await callback.message.delete()

# --- Admin Settings ---
@router.callback_query(F.data == "admin_settings")
async def admin_settings_menu(callback: CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.button(text="Change Welcome", callback_data="admin_set_welcome")
    builder.button(text="Change Currency", callback_data="admin_set_currency")
    builder.button(text="Change Support", callback_data="admin_set_support")
    builder.button(text="Change Rules", callback_data="admin_set_rules")
    builder.button(text="Toggle Captcha", callback_data="admin_toggle_captcha")
    builder.button(text="Toggle Shop", callback_data="admin_toggle_shop")
    builder.button(text="Set Referral Reward", callback_data="admin_set_ref_reward")
    builder.button(text="Set Daily Reward", callback_data="admin_set_daily_reward")
    builder.button(text="Toggle Daily", callback_data="admin_toggle_daily")
    builder.button(text="Set Scratch Rewards", callback_data="admin_set_scratch_rewards")
    builder.button(text="Toggle Scratch", callback_data="admin_toggle_scratch")
    builder.button(text="Set Force Join", callback_data="admin_set_force_join")
    builder.button(text="🔙 Back", callback_data="admin_panel")
    builder.adjust(2)
    await callback.message.edit_text("⚙️ Bot Settings", reply_markup=builder.as_markup())

@router.callback_query(F.data.startswith("admin_set_"))
async def admin_set_start(callback: CallbackQuery, state: FSMContext):
    key_map = {
        "admin_set_welcome": ("welcome_message", "New Welcome Message (use {name}):"),
        "admin_set_currency": ("currency", "New Currency Symbol:"),
        "admin_set_support": ("support_link", "New Support Link (URL):"),
        "admin_set_rules": ("rules", "New Rules Text:"),
        "admin_set_ref_reward": ("referral_reward", "New Fixed Referral Reward:"),
        "admin_set_daily_reward": ("daily_reward", "New Daily Bonus Amount:"),
        "admin_set_scratch_rewards": ("scratch_rewards", "New Scratch Rewards (comma-separated):"),
        "admin_set_force_join": ("channel_force_join", "New Force Join Channel (ID or @username):"),
    }
    if callback.data in key_map:
        key, prompt = key_map[callback.data]
        await state.update_data(setting_key=key)
        await callback.message.answer(prompt)
        await state.set_state(AdminStates.waiting_for_setting_value)
    elif callback.data == "admin_toggle_captcha":
        current = await get_setting("captcha_enabled")
        new = "0" if current == "1" else "1"
        await update_setting("captcha_enabled", new)
        await callback.answer(f"Captcha toggled.")
        await admin_settings_menu(callback)
    elif callback.data == "admin_toggle_shop":
        current = await get_setting("shop_enabled")
        new = "0" if current == "1" else "1"
        await update_setting("shop_enabled", new)
        await callback.answer(f"Shop toggled.")
        await admin_settings_menu(callback)
    elif callback.data == "admin_toggle_daily":
        current = await get_setting("daily_enabled")
        new = "0" if current == "1" else "1"
        await update_setting("daily_enabled", new)
        await callback.answer(f"Daily bonus toggled.")
        await admin_settings_menu(callback)
    elif callback.data == "admin_toggle_scratch":
        current = await get_setting("scratch_enabled")
        new = "0" if current == "1" else "1"
        await update_setting("scratch_enabled", new)
        await callback.answer(f"Scratch card toggled.")
        await admin_settings_menu(callback)

@router.message(AdminStates.waiting_for_setting_value)
async def admin_set_value(message: types.Message, state: FSMContext):
    data = await state.get_data()
    key = data['setting_key']
    value = message.text
    await update_setting(key, value)
    await message.answer(f"✅ Setting '{key}' updated to: `{value}`", reply_markup=admin_panel_kb(), parse_mode=ParseMode.MARKDOWN)
    await state.clear()

# --- Admin Promos ---
@router.callback_query(F.data == "admin_promos")
async def admin_promos_menu(callback: CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Create Promo", callback_data="admin_create_promo")
    builder.button(text="🔙 Back", callback_data="admin_panel")
    await callback.message.edit_text("🎁 Promo Codes Management", reply_markup=builder.as_markup())

@router.callback_query(F.data == "admin_create_promo")
async def admin_create_promo(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("📝 Enter Promo Code:")
    await state.set_state(AdminStates.waiting_for_promo_code)

@router.message(AdminStates.waiting_for_promo_code)
async def admin_promo_code(message: types.Message, state: FSMContext):
    code = message.text.strip()
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT * FROM promos WHERE code = ?", (code,)) as cursor:
            if await cursor.fetchone():
                await message.answer("Code already exists. Try another one:")
                return
    await state.update_data(code=code)
    await message.answer("💰 Reward Amount:")
    await state.set_state(AdminStates.waiting_for_promo_reward)

@router.message(AdminStates.waiting_for_promo_reward)
async def admin_promo_reward(message: types.Message, state: FSMContext):
    try:
        reward = float(message.text)
        await state.update_data(reward=reward)
        await message.answer("🔢 Max Usage (-1 for unlimited):")
        await state.set_state(AdminStates.waiting_for_promo_limit)
    except ValueError:
        await message.answer("Invalid number.")

@router.message(AdminStates.waiting_for_promo_limit)
async def admin_promo_limit(message: types.Message, state: FSMContext):
    try:
        limit = int(message.text)
        data = await state.get_data()

        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("INSERT INTO promos (code, reward, max_usage, expiry_date) VALUES (?, ?, ?, ?)",
                             (data['code'], data['reward'], limit, "2099-01-01"))
            await db.commit()

        await message.answer(f"✅ Promo '{data['code']}' Created!", reply_markup=admin_panel_kb())
        await state.clear()
    except ValueError:
        await message.answer("Invalid number.")

# --- Admin Tasks ---
@router.callback_query(F.data == "admin_tasks")
async def admin_tasks_menu(callback: CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Create Task", callback_data="admin_create_task")
    builder.button(text="🔙 Back", callback_data="admin_panel")
    await callback.message.edit_text("📋 Task Management", reply_markup=builder.as_markup())

@router.callback_query(F.data == "admin_create_task")
async def admin_create_task(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("📝 Task Description:")
    await state.set_state(AdminStates.waiting_for_task_desc)

@router.message(AdminStates.waiting_for_task_desc)
async def admin_task_desc(message: types.Message, state: FSMContext):
    await state.update_data(desc=message.text)
    await message.answer("🔗 Task Link (or 'None'):")
    await state.set_state(AdminStates.waiting_for_task_link)

@router.message(AdminStates.waiting_for_task_link)
async def admin_task_link(message: types.Message, state: FSMContext):
    await state.update_data(link=message.text)
    await message.answer("💰 Reward Amount:")
    await state.set_state(AdminStates.waiting_for_task_reward)

@router.message(AdminStates.waiting_for_task_reward)
async def admin_task_reward(message: types.Message, state: FSMContext):
    try:
        reward = float(message.text)
        data = await state.get_data()

        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("INSERT INTO tasks (description, link, reward) VALUES (?, ?, ?)",
                             (data['desc'], data['link'], reward))
            await db.commit()

        await message.answer("✅ Task Created!", reply_markup=admin_panel_kb())
        await state.clear()
    except ValueError:
        await message.answer("Invalid number.")

# --- Start Bot in Background Thread ---
def run_bot():
    asyncio.run(main())

async def main():
    if not TOKEN:
        print("❌ Bot token not found.")
        return
    if not ADMIN_ID:
        print("❌ Admin ID not found.")
        return

    await init_db()

    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    print("🤖 Bot is starting...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

# --- Flask entry point for Render ---
if __name__ == "__main__":
    # Start bot in background thread
    threading.Thread(target=run_bot, daemon=True).start()
    # Run Flask server
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
