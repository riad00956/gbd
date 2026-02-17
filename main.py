import asyncio
import logging
import os
import sys
import random
import zipfile
import io
from datetime import datetime, date
from typing import Optional

from flask import Flask, jsonify
import threading

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
    FSInputFile,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# ---------- Configuration ----------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")
DB_NAME = "shop_bot.db"
PORT = int(os.getenv("PORT", 10000))  # Render uses PORT env var

# ---------- Flask App for Health Check ----------
app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({
        "status": "running",
        "bot": "Telegram Shop Bot",
        "version": "2.0",
        "timestamp": datetime.now().isoformat()
    })

@app.route('/health')
def health():
    return jsonify({"status": "healthy"}), 200

def run_flask():
    app.run(host='0.0.0.0', port=PORT)

# ---------- Database Initialization ----------
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
            "welcome_message": "🌟 Welcome to *Premium Shop*, {name}!",
            "currency": "💎 Credits",
            "support_link": "https://t.me/telegram",
            "rules": "📜 **Rules:**\n1. No spamming\n2. Be respectful\n3. Enjoy your shopping!",
            "channel_force_join": "",
            "captcha_enabled": "1",
            "shop_enabled": "1",
            "referral_reward": "5.0",
            "referral_type": "fixed",
            # Daily Bonus
            "daily_bonus_enabled": "1",
            "daily_bonus_amount": "10",
            # Scratch Card
            "scratch_enabled": "1",
            "scratch_rewards": "5,10,15,20,25",
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
                banned INTEGER DEFAULT 0
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
                image_file_id TEXT,
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

        # Transactions
        await db.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL,
                type TEXT,
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Daily Bonus tracking
        await db.execute("""
            CREATE TABLE IF NOT EXISTS daily_bonus (
                user_id INTEGER PRIMARY KEY,
                last_claim DATE
            )
        """)

        # Daily Scratch tracking
        await db.execute("""
            CREATE TABLE IF NOT EXISTS daily_scratch (
                user_id INTEGER PRIMARY KEY,
                last_scratch DATE
            )
        """)

        await db.commit()

# ---------- Database Helpers ----------
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
            reward = float(await get_setting("referral_reward") or 0)
            await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (reward, referrer_id))
            await db.execute("""
                INSERT INTO transactions (user_id, amount, type, description)
                VALUES (?, ?, 'referral', ?)
            """, (referrer_id, reward, f"Referral bonus from {full_name}"))
        
        await db.commit()

async def notify_user(user_id: int, text: str, bot: Bot):
    try:
        await bot.send_message(user_id, f"🔔 {text}")
    except Exception:
        pass

# ---------- Backup Function ----------
async def create_backup(bot: Bot, chat_id: int):
    """Create a zip backup of the entire database and send it."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_filename = f"backup_{timestamp}.zip"
    
    # Create in-memory zip file
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        # Add database file
        if os.path.exists(DB_NAME):
            zip_file.write(DB_NAME, arcname=DB_NAME)
        
        # Add a README with backup info
        readme_content = f"""Backup created at: {datetime.now()}
Database file: {DB_NAME}
Bot: @{bot._me.username if bot._me else 'unknown'}
Admin: {ADMIN_ID}
"""
        zip_file.writestr("README.txt", readme_content)
    
    zip_buffer.seek(0)
    
    # Send as document
    await bot.send_document(
        chat_id,
        types.BufferedInputFile(zip_buffer.getvalue(), filename=backup_filename),
        caption=f"📦 **Database Backup**\nCreated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        parse_mode=ParseMode.MARKDOWN
    )

# ---------- FSM States ----------
class AdminStates(StatesGroup):
    # Broadcast
    waiting_for_broadcast = State()
    
    # Category
    waiting_for_category_name = State()
    waiting_for_category_edit = State()
    
    # Product
    waiting_for_product_category = State()
    waiting_for_product_type = State()
    waiting_for_product_name = State()
    waiting_for_product_desc = State()
    waiting_for_product_price = State()
    waiting_for_product_stock = State()
    waiting_for_product_content = State()
    waiting_for_product_image = State()
    waiting_for_product_edit_field = State()
    waiting_for_product_edit_value = State()
    
    # User
    waiting_for_user_search = State()
    waiting_for_balance_change = State()
    
    # Settings
    waiting_for_setting_key = State()
    waiting_for_setting_value = State()
    
    # Tasks
    waiting_for_task_desc = State()
    waiting_for_task_link = State()
    waiting_for_task_reward = State()
    waiting_for_task_edit = State()
    
    # Promos
    waiting_for_promo_code = State()
    waiting_for_promo_reward = State()
    waiting_for_promo_limit = State()
    waiting_for_promo_expiry = State()
    waiting_for_promo_edit = State()

class ShopStates(StatesGroup):
    captcha = State()
    waiting_for_address = State()

class UserStates(StatesGroup):
    waiting_for_promo = State()

# ---------- Keyboards ----------
def main_menu_kb(is_admin=False):
    builder = ReplyKeyboardBuilder()
    builder.row(
        KeyboardButton(text="🛍 Shop"),
        KeyboardButton(text="👤 Profile"),
        KeyboardButton(text="🎁 Daily Bonus")
    )
    builder.row(
        KeyboardButton(text="🎲 Scratch Card"),
        KeyboardButton(text="📋 Tasks"),
        KeyboardButton(text="ℹ️ Support")
    )
    builder.row(KeyboardButton(text="📜 Rules"))
    if is_admin:
        builder.row(KeyboardButton(text="⚙️ Admin Panel"))
    return builder.as_markup(resize_keyboard=True)

def admin_panel_kb():
    builder = InlineKeyboardBuilder()
    builder.button(text="📊 Stats", callback_data="admin_stats")
    builder.button(text="👥 Users", callback_data="admin_users")
    builder.button(text="📢 Broadcast", callback_data="admin_broadcast")
    builder.button(text="🛍 Shop Mgmt", callback_data="admin_shop")
    builder.button(text="⚙️ Settings", callback_data="admin_settings")
    builder.button(text="🎁 Promos", callback_data="admin_promos")
    builder.button(text="📋 Tasks Mgmt", callback_data="admin_tasks")
    builder.button(text="📦 Orders", callback_data="admin_orders")
    builder.button(text="📦 Backup DB", callback_data="admin_backup")
    builder.adjust(2)
    return builder.as_markup()

def shop_mgmt_kb():
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Add Category", callback_data="admin_add_cat")
    builder.button(text="📋 List Categories", callback_data="admin_list_cats")
    builder.button(text="🔙 Back", callback_data="admin_panel")
    builder.adjust(1)
    return builder.as_markup()

# ---------- Emoji Captcha ----------
EMOJI_LIST = ["😀", "😂", "😍", "🥺", "😎", "😡", "🎉", "🔥", "⭐", "🍕", "🍔", "🚗", "🐶", "🐱", "🐭", "🐹", "🐰", "🦊", "🐻", "🐼"]

def generate_emoji_captcha():
    seq = random.sample(EMOJI_LIST, 4)
    missing_index = random.randint(0, 3)
    answer = seq[missing_index]
    seq[missing_index] = "___"
    question = " ".join(seq)
    return question, answer

# ---------- Router ----------
router = Router()

# ---------- Start & Captcha ----------
@router.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    args = message.text.split()
    referrer_id = None
    if len(args) > 1 and args[1].isdigit():
        ref = int(args[1])
        if ref != message.from_user.id:
            if await get_user(ref):
                referrer_id = ref

    await add_user(message.from_user.id, message.from_user.username, message.from_user.full_name, referrer_id)

    # Force join check
    channel = await get_setting("channel_force_join")
    if channel:
        try:
            member = await message.bot.get_chat_member(channel, message.from_user.id)
            if member.status == "left":
                await message.answer(f"🚫 **Access Denied**\nYou must join our channel first:\n{channel}", parse_mode=ParseMode.MARKDOWN)
                return
        except:
            pass

    captcha_enabled = await get_setting("captcha_enabled")
    if captcha_enabled == "1":
        question, answer = generate_emoji_captcha()
        await state.update_data(captcha_answer=answer)
        await state.set_state(ShopStates.captcha)
        await message.answer(
            f"🔒 **Security Check**\n\nComplete the emoji sequence:\n`{question}`\n\nType the missing emoji:",
            reply_markup=types.ReplyKeyboardRemove(),
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await show_welcome(message)

@router.message(ShopStates.captcha)
async def process_captcha(message: types.Message, state: FSMContext):
    data = await state.get_data()
    if message.text.strip() == data.get("captcha_answer"):
        await state.clear()
        await show_welcome(message)
    else:
        await message.answer("❌ **Incorrect.** Try again.", parse_mode=ParseMode.MARKDOWN)
        question, answer = generate_emoji_captcha()
        await state.update_data(captcha_answer=answer)
        await message.answer(f"🔄 New sequence:\n`{question}`\n\nType the missing emoji:", parse_mode=ParseMode.MARKDOWN)

async def show_welcome(message: types.Message):
    welcome_text = await get_setting("welcome_message")
    user = await get_user(message.from_user.id)
    is_admin = str(message.from_user.id) == str(ADMIN_ID)

    if user and user['banned']:
        await message.answer("🚫 **You are banned** from this bot.", parse_mode=ParseMode.MARKDOWN)
        return

    welcome_text = welcome_text.replace("{name}", message.from_user.full_name)
    await message.answer(
        welcome_text,
        reply_markup=main_menu_kb(is_admin),
        parse_mode=ParseMode.MARKDOWN
    )

# ---------- Profile ----------
@router.message(F.text == "👤 Profile")
async def profile(message: types.Message):
    user = await get_user(message.from_user.id)
    currency = await get_setting("currency")
    if not user:
        await message.answer("❌ User not found. Try /start", parse_mode=ParseMode.MARKDOWN)
        return

    spent = user['total_spent']
    if spent < 100:
        level = "🥉 Bronze"
    elif spent < 500:
        level = "🥈 Silver"
    else:
        level = "🥇 Gold"

    text = (
        f"👤 **Your Profile**\n\n"
        f"🆔 ID: `{user['user_id']}`\n"
        f"💰 **Balance:** `{user['balance']} {currency}`\n"
        f"📊 **Total Spent:** `{spent} {currency}`\n"
        f"🏅 **Level:** {level}\n"
        f"📅 **Joined:** {user['joined_at']}\n"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="📦 My Orders", callback_data="my_orders")
    builder.button(text="📜 Transaction History", callback_data="my_transactions")
    builder.button(text="🎁 Redeem Promo", callback_data="redeem_promo")
    bot_info = await message.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={user['user_id']}"

    await message.answer(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.MARKDOWN)
    await message.answer(f"🔗 **Referral Link:**\n`{ref_link}`\n\nShare this link to earn rewards!", parse_mode=ParseMode.MARKDOWN)

@router.callback_query(F.data == "my_transactions")
async def my_transactions(callback: CallbackQuery):
    user_id = callback.from_user.id
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT * FROM transactions 
            WHERE user_id = ? 
            ORDER BY created_at DESC LIMIT 10
        """, (user_id,)) as cursor:
            txs = await cursor.fetchall()

    if not txs:
        await callback.message.answer("📭 No transactions yet.", parse_mode=ParseMode.MARKDOWN)
        return

    lines = ["📊 **Last 10 Transactions:**\n"]
    for tx in txs:
        sign = "+" if tx['amount'] > 0 else ""
        lines.append(f"• {tx['created_at'][:10]} `{tx['type']}`: {sign}{tx['amount']} Credits\n  _{tx['description']}_")
    await callback.message.answer("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

@router.message(F.text == "ℹ️ Support")
async def support(message: types.Message):
    link = await get_setting("support_link")
    await message.answer(f"ℹ️ **Contact Support:** {link}", parse_mode=ParseMode.MARKDOWN)

@router.message(F.text == "📜 Rules")
async def rules(message: types.Message):
    rules_text = await get_setting("rules")
    await message.answer(rules_text, parse_mode=ParseMode.MARKDOWN)

# ---------- Daily Bonus ----------
@router.message(F.text == "🎁 Daily Bonus")
async def daily_bonus(message: types.Message, bot: Bot):
    enabled = await get_setting("daily_bonus_enabled")
    if enabled != "1":
        await message.answer("❌ Daily bonus is currently disabled.", parse_mode=ParseMode.MARKDOWN)
        return

    user_id = message.from_user.id
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT last_claim FROM daily_bonus WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row and row[0] == today:
                await message.answer("⏳ You already claimed your daily bonus today. Come back tomorrow!", parse_mode=ParseMode.MARKDOWN)
                return

        amount = float(await get_setting("daily_bonus_amount"))
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
        await db.execute("INSERT OR REPLACE INTO daily_bonus (user_id, last_claim) VALUES (?, ?)", (user_id, today))
        await db.execute("""
            INSERT INTO transactions (user_id, amount, type, description)
            VALUES (?, ?, 'daily_bonus', 'Daily bonus')
        """, (user_id, amount))
        await db.commit()

    await message.answer(f"🎉 **Daily Bonus Claimed!**\nYou received `{amount}` Credits.", parse_mode=ParseMode.MARKDOWN)
    await notify_user(user_id, f"Daily bonus of {amount} Credits added!", bot)

# ---------- Scratch Card ----------
@router.message(F.text == "🎲 Scratch Card")
async def scratch_card(message: types.Message, bot: Bot):
    enabled = await get_setting("scratch_enabled")
    if enabled != "1":
        await message.answer("❌ Scratch card is currently disabled.", parse_mode=ParseMode.MARKDOWN)
        return

    user_id = message.from_user.id
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT last_scratch FROM daily_scratch WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row and row[0] == today:
                await message.answer("⏳ You already scratched today. Come back tomorrow!", parse_mode=ParseMode.MARKDOWN)
                return

        rewards_str = await get_setting("scratch_rewards")
        try:
            rewards = [float(x.strip()) for x in rewards_str.split(",") if x.strip()]
        except:
            rewards = [5, 10, 15, 20, 25]

        if not rewards:
            await message.answer("❌ No scratch rewards configured. Contact admin.", parse_mode=ParseMode.MARKDOWN)
            return

        amount = random.choice(rewards)
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
        await db.execute("INSERT OR REPLACE INTO daily_scratch (user_id, last_scratch) VALUES (?, ?)", (user_id, today))
        await db.execute("""
            INSERT INTO transactions (user_id, amount, type, description)
            VALUES (?, ?, 'scratch', 'Daily scratch card')
        """, (user_id, amount))
        await db.commit()

    await message.answer(f"🎲 **You scratched and won!**\n`{amount}` Credits added to your balance.", parse_mode=ParseMode.MARKDOWN)
    await notify_user(user_id, f"You won {amount} Credits from scratch card!", bot)

# ---------- Tasks ----------
@router.message(F.text == "📋 Tasks")
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
        await message.answer("✅ No new tasks available.", parse_mode=ParseMode.MARKDOWN)
        return

    text = "📋 **Available Tasks:**\n\n"
    builder = InlineKeyboardBuilder()

    for task in tasks:
        text += f"🔹 `{task['description']}` – Reward: `{task['reward']}` Credits\n"
        builder.button(text=f"✅ Complete Task", callback_data=f"do_task_{task['id']}")

    builder.adjust(1)
    await message.answer(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.MARKDOWN)

@router.callback_query(F.data.startswith("do_task_"))
async def do_task(callback: CallbackQuery, bot: Bot):
    task_id = int(callback.data.split("_")[2])
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)) as cursor:
            task = await cursor.fetchone()

    if not task:
        await callback.answer("❌ Task not found.")
        return

    user_id = callback.from_user.id

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT * FROM completed_tasks WHERE user_id = ? AND task_id = ?", (user_id, task_id)) as cursor:
            if await cursor.fetchone():
                await callback.answer("⏳ Already completed!")
                return

        await db.execute("INSERT INTO completed_tasks (user_id, task_id) VALUES (?, ?)", (user_id, task_id))
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (task['reward'], user_id))
        await db.execute("""
            INSERT INTO transactions (user_id, amount, type, description)
            VALUES (?, ?, 'task', ?)
        """, (user_id, task['reward'], task['description']))
        await db.commit()

    await callback.message.answer(f"✅ **Task Completed!**\nYou earned `{task['reward']}` Credits.", parse_mode=ParseMode.MARKDOWN)
    await notify_user(user_id, f"Task completed: {task['description']}. You earned {task['reward']} Credits!", bot)

# ---------- Promo Codes ----------
@router.callback_query(F.data == "redeem_promo")
async def redeem_promo_ask(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("🎁 **Enter your Promo Code:**", parse_mode=ParseMode.MARKDOWN)
    await state.set_state(UserStates.waiting_for_promo)

@router.message(UserStates.waiting_for_promo)
async def redeem_promo_process(message: types.Message, state: FSMContext, bot: Bot):
    code = message.text.strip()
    user_id = message.from_user.id

    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM promos WHERE code = ?", (code,)) as cursor:
            promo = await cursor.fetchone()

        if not promo:
            await message.answer("❌ **Invalid code.**", parse_mode=ParseMode.MARKDOWN)
            await state.clear()
            return

        if promo['expiry_date'] and promo['expiry_date'] < datetime.now().strftime("%Y-%m-%d"):
            await message.answer("❌ **Code expired.**", parse_mode=ParseMode.MARKDOWN)
            await state.clear()
            return

        if promo['max_usage'] != -1 and promo['used_count'] >= promo['max_usage']:
            await message.answer("❌ **Code limit reached.**", parse_mode=ParseMode.MARKDOWN)
            await state.clear()
            return

        async with db.execute("SELECT * FROM promo_usage WHERE user_id = ? AND code = ?", (user_id, code)) as cursor:
            if await cursor.fetchone():
                await message.answer("❌ **You already used this code.**", parse_mode=ParseMode.MARKDOWN)
                await state.clear()
                return

        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (promo['reward'], user_id))
        await db.execute("UPDATE promos SET used_count = used_count + 1 WHERE code = ?", (code,))
        await db.execute("INSERT INTO promo_usage (user_id, code) VALUES (?, ?)", (user_id, code))
        await db.execute("""
            INSERT INTO transactions (user_id, amount, type, description)
            VALUES (?, ?, 'promo', ?)
        """, (user_id, promo['reward'], f"Promo code {code}"))
        await db.commit()

    await message.answer(f"✅ **Code Redeemed!**\nYou received `{promo['reward']}` Credits.", parse_mode=ParseMode.MARKDOWN)
    await notify_user(user_id, f"Promo code {code} redeemed! You received {promo['reward']} Credits.", bot)
    await state.clear()

# ---------- Shop ----------
@router.message(F.text == "🛍 Shop")
async def shop_entry(message: types.Message):
    shop_enabled = await get_setting("shop_enabled")
    if shop_enabled == "0":
        await message.answer("⚠️ **Shop is currently disabled.**", parse_mode=ParseMode.MARKDOWN)
        return

    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM categories") as cursor:
            categories = await cursor.fetchall()

    if not categories:
        await message.answer("📭 No categories available.", parse_mode=ParseMode.MARKDOWN)
        return

    builder = InlineKeyboardBuilder()
    for cat in categories:
        builder.button(text=cat['name'], callback_data=f"cat_{cat['id']}")
    builder.adjust(2)
    await message.answer("📂 **Select a Category:**", reply_markup=builder.as_markup(), parse_mode=ParseMode.MARKDOWN)

@router.callback_query(F.data.startswith("cat_"))
async def show_products(callback: CallbackQuery):
    cat_id = int(callback.data.split("_")[1])
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM products WHERE category_id = ?", (cat_id,)) as cursor:
            products = await cursor.fetchall()

    if not products:
        await callback.message.answer("📭 No products in this category.", parse_mode=ParseMode.MARKDOWN)
        return

    builder = InlineKeyboardBuilder()
    for prod in products:
        stock_text = "∞" if prod['stock'] == -1 else prod['stock']
        builder.button(text=f"{prod['name']} | {prod['price']} | Stock: {stock_text}", callback_data=f"prod_{prod['id']}")
    builder.adjust(1)
    await callback.message.answer("📦 **Available Products:**", reply_markup=builder.as_markup(), parse_mode=ParseMode.MARKDOWN)

@router.callback_query(F.data.startswith("prod_"))
async def show_product_details(callback: CallbackQuery):
    prod_id = int(callback.data.split("_")[1])
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM products WHERE id = ?", (prod_id,)) as cursor:
            prod = await cursor.fetchone()

    if not prod:
        await callback.message.answer("❌ Product not found.", parse_mode=ParseMode.MARKDOWN)
        return

    currency = await get_setting("currency")
    text = (
        f"📦 **{prod['name']}**\n\n"
        f"📝 {prod['description']}\n\n"
        f"💰 **Price:** `{prod['price']} {currency}`\n"
        f"📦 **Stock:** `{'Unlimited' if prod['stock'] == -1 else prod['stock']}`\n"
        f"🔄 **Type:** `{prod['type']}`"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="💳 Buy Now", callback_data=f"buy_{prod['id']}")
    builder.button(text="🔙 Back", callback_data="shop_main")
    await callback.message.answer(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.MARKDOWN)

@router.callback_query(F.data.startswith("buy_"))
async def buy_product(callback: CallbackQuery, state: FSMContext, bot: Bot):
    prod_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id

    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM products WHERE id = ?", (prod_id,)) as cursor:
            prod = await cursor.fetchone()
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            user = await cursor.fetchone()

    if not prod:
        await callback.answer("❌ Product not found.")
        return
    if prod['stock'] != -1 and prod['stock'] <= 0:
        await callback.answer("❌ Out of stock!")
        return
    if user['balance'] < prod['price']:
        await callback.answer("❌ Insufficient balance!")
        return

    if prod['type'] == 'physical':
        await state.update_data(buy_prod_id=prod_id)
        await callback.message.answer("📦 **Please enter your shipping address:**", parse_mode=ParseMode.MARKDOWN)
        await state.set_state(ShopStates.waiting_for_address)
        return

    await process_purchase(user_id, prod, callback.message, bot)

async def process_purchase(user_id: int, prod: aiosqlite.Row, message: Message, bot: Bot, address: str = None):
    new_balance = user['balance'] - prod['price']
    new_stock = prod['stock'] - 1 if prod['stock'] != -1 else -1

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET balance = ?, total_spent = total_spent + ? WHERE user_id = ?",
                         (new_balance, prod['price'], user_id))
        if new_stock != -1:
            await db.execute("UPDATE products SET stock = ? WHERE id = ?", (new_stock, prod['id']))

        status = "delivered" if prod['type'] in ['digital', 'file'] else "pending"
        order_data = prod['content'] if status == "delivered" else (address or "Waiting for manual processing")
        await db.execute("INSERT INTO orders (user_id, product_id, status, data) VALUES (?, ?, ?, ?)",
                         (user_id, prod['id'], status, order_data))
        await db.execute("""
            INSERT INTO transactions (user_id, amount, type, description)
            VALUES (?, ?, 'purchase', ?)
        """, (user_id, -prod['price'], f"Bought {prod['name']}"))
        await db.commit()

    await message.answer(f"✅ **Purchase Successful!**\nNew Balance: `{new_balance}` {await get_setting('currency')}", parse_mode=ParseMode.MARKDOWN)
    await notify_user(user_id, f"You successfully purchased {prod['name']}!", bot)

    if prod['type'] == 'file':
        try:
            await bot.send_document(user_id, prod['content'], caption=f"📁 **{prod['name']}**", parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            await message.answer(f"❌ Failed to send file. Error: {e}", parse_mode=ParseMode.MARKDOWN)
    elif prod['type'] == 'digital':
        await message.answer(f"📦 **Your Item:**\n`{prod['content']}`", parse_mode=ParseMode.MARKDOWN)
    elif prod['type'] == 'physical':
        await message.answer("📦 Order placed! An admin will review it shortly.", parse_mode=ParseMode.MARKDOWN)

@router.message(ShopStates.waiting_for_address)
async def receive_address(message: types.Message, state: FSMContext, bot: Bot):
    address = message.text
    data = await state.get_data()
    prod_id = data['buy_prod_id']
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM products WHERE id = ?", (prod_id,)) as cursor:
            prod = await cursor.fetchone()
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (message.from_user.id,)) as cursor:
            user = await cursor.fetchone()
    await process_purchase(message.from_user.id, prod, message, bot, address)
    await state.clear()

@router.callback_query(F.data == "my_orders")
async def my_orders(callback: CallbackQuery):
    user_id = callback.from_user.id
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT orders.*, products.name 
            FROM orders 
            JOIN products ON orders.product_id = products.id 
            WHERE user_id = ? 
            ORDER BY created_at DESC LIMIT 5
        """, (user_id,)) as cursor:
            orders = await cursor.fetchall()
    if not orders:
        await callback.message.answer("📭 No orders found.", parse_mode=ParseMode.MARKDOWN)
        return
    text = "📦 **Recent Orders:**\n\n"
    for order in orders:
        text += f"🔹 `{order['name']}` – **{order['status'].upper()}**\n"
    await callback.message.answer(text, parse_mode=ParseMode.MARKDOWN)

# ---------- Admin Panel ----------
@router.message(F.text == "⚙️ Admin Panel")
async def admin_panel_entry(message: types.Message):
    if str(message.from_user.id) != str(ADMIN_ID):
        return
    await message.answer("🔧 **Admin Control Center**", reply_markup=admin_panel_kb(), parse_mode=ParseMode.MARKDOWN)

@router.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cursor:
            total_users = (await cursor.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM orders") as cursor:
            total_orders = (await cursor.fetchone())[0]
        async with db.execute("SELECT SUM(price) FROM products JOIN orders ON products.id = orders.product_id") as cursor:
            revenue = (await cursor.fetchone())[0] or 0.0

    text = (
        "📊 **Live Statistics**\n\n"
        f"👥 **Users:** `{total_users}`\n"
        f"📦 **Orders:** `{total_orders}`\n"
        f"💰 **Revenue:** `{revenue} {await get_setting('currency')}`\n"
    )
    await callback.message.edit_text(text, reply_markup=admin_panel_kb(), parse_mode=ParseMode.MARKDOWN)

@router.callback_query(F.data == "admin_backup")
async def admin_backup(callback: CallbackQuery):
    await callback.message.answer("📦 **Creating backup...**", parse_mode=ParseMode.MARKDOWN)
    await create_backup(callback.bot, callback.from_user.id)
    await callback.answer()

@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_ask(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("📝 **Send the message you want to broadcast** (Text, Photo, or Video):", parse_mode=ParseMode.MARKDOWN)
    await state.set_state(AdminStates.waiting_for_broadcast)

@router.message(AdminStates.waiting_for_broadcast)
async def admin_broadcast_send(message: types.Message, state: FSMContext, bot: Bot):
    await state.clear()
    msg = await message.answer("⏳ **Starting broadcast...**", parse_mode=ParseMode.MARKDOWN)

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM users") as cursor:
            users = await cursor.fetchall()

    count = 0
    for user in users:
        try:
            await message.copy_to(user[0])
            count += 1
            if count % 10 == 0:
                await asyncio.sleep(0.5)
        except Exception:
            pass

    await msg.edit_text(f"✅ **Broadcast finished.** Sent to `{count}` users.", parse_mode=ParseMode.MARKDOWN)

# ---------- Shop Management ----------
@router.callback_query(F.data == "admin_shop")
async def admin_shop_menu(callback: CallbackQuery):
    await callback.message.edit_text("🛍 **Shop Management**", reply_markup=shop_mgmt_kb(), parse_mode=ParseMode.MARKDOWN)

@router.callback_query(F.data == "admin_add_cat")
async def admin_add_cat(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("📝 **Enter new Category Name:**", parse_mode=ParseMode.MARKDOWN)
    await state.set_state(AdminStates.waiting_for_category_name)

@router.message(AdminStates.waiting_for_category_name)
async def admin_save_cat(message: types.Message, state: FSMContext):
    name = message.text
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (name,))
        await db.commit()
    await message.answer(f"✅ **Category '{name}' added.**", parse_mode=ParseMode.MARKDOWN)
    await state.clear()

@router.callback_query(F.data == "admin_list_cats")
async def admin_list_cats(callback: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM categories") as cursor:
            cats = await cursor.fetchall()
    if not cats:
        await callback.message.answer("📭 No categories.", parse_mode=ParseMode.MARKDOWN)
        return
    builder = InlineKeyboardBuilder()
    for cat in cats:
        builder.row(
            InlineKeyboardButton(text=f"📁 {cat['name']}", callback_data=f"admin_cat_{cat['id']}"),
            InlineKeyboardButton(text="✏️", callback_data=f"admin_edit_cat_{cat['id']}"),
            InlineKeyboardButton(text="🗑️", callback_data=f"admin_del_cat_{cat['id']}")
        )
    builder.row(InlineKeyboardButton(text="🔙 Back", callback_data="admin_shop"))
    await callback.message.edit_text("📂 **Categories:**", reply_markup=builder.as_markup(), parse_mode=ParseMode.MARKDOWN)

@router.callback_query(F.data.startswith("admin_cat_"))
async def admin_cat_products(callback: CallbackQuery):
    cat_id = int(callback.data.split("_")[2])
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM products WHERE category_id = ?", (cat_id,)) as cursor:
            prods = await cursor.fetchall()
    if not prods:
        await callback.message.answer("📭 No products in this category.", parse_mode=ParseMode.MARKDOWN)
        return
    builder = InlineKeyboardBuilder()
    for prod in prods:
        builder.row(
            InlineKeyboardButton(text=f"{prod['name']} | {prod['price']}", callback_data=f"admin_prod_{prod['id']}"),
            InlineKeyboardButton(text="✏️", callback_data=f"admin_edit_prod_{prod['id']}"),
            InlineKeyboardButton(text="🗑️", callback_data=f"admin_del_prod_{prod['id']}")
        )
    builder.row(InlineKeyboardButton(text="➕ Add Product", callback_data=f"admin_add_prod_cat_{cat_id}"))
    builder.row(InlineKeyboardButton(text="🔙 Back", callback_data="admin_list_cats"))
    await callback.message.edit_text("📦 **Products:**", reply_markup=builder.as_markup(), parse_mode=ParseMode.MARKDOWN)

@router.callback_query(F.data.startswith("admin_add_prod_cat_"))
async def admin_add_prod_start(callback: CallbackQuery, state: FSMContext):
    cat_id = int(callback.data.split("_")[4])
    await state.update_data(cat_id=cat_id)
    await callback.message.answer("📝 **Product Name:**", parse_mode=ParseMode.MARKDOWN)
    await state.set_state(AdminStates.waiting_for_product_name)

# Product creation states
@router.message(AdminStates.waiting_for_product_name)
async def admin_prod_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("📝 **Description:**", parse_mode=ParseMode.MARKDOWN)
    await state.set_state(AdminStates.waiting_for_product_desc)

@router.message(AdminStates.waiting_for_product_desc)
async def admin_prod_desc(message: types.Message, state: FSMContext):
    await state.update_data(desc=message.text)
    await message.answer("💰 **Price:**", parse_mode=ParseMode.MARKDOWN)
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
        await message.answer("📦 **Product Type:**", reply_markup=builder.as_markup(one_time_keyboard=True), parse_mode=ParseMode.MARKDOWN)
        await state.set_state(AdminStates.waiting_for_product_type)
    except ValueError:
        await message.answer("❌ **Invalid price.** Enter a number.", parse_mode=ParseMode.MARKDOWN)

@router.message(AdminStates.waiting_for_product_type)
async def admin_prod_type(message: types.Message, state: FSMContext):
    ptype = message.text.lower()
    if ptype not in ['digital', 'file', 'physical']:
        await message.answer("❌ **Invalid type.**", parse_mode=ParseMode.MARKDOWN)
        return
    await state.update_data(type=ptype)
    if ptype == 'physical':
        await state.update_data(content="Physical Item")
        await message.answer("📦 **Stock amount** (-1 for unlimited):", reply_markup=types.ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN)
        await state.set_state(AdminStates.waiting_for_product_stock)
    else:
        await message.answer("📄 **Content** (for digital: text; for file: upload the file now):", reply_markup=types.ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN)
        await state.set_state(AdminStates.waiting_for_product_content)

@router.message(AdminStates.waiting_for_product_content, F.content_type.in_({'text', 'document', 'photo', 'video'}))
async def admin_prod_content(message: types.Message, state: FSMContext):
    if message.document:
        content = message.document.file_id
    elif message.photo:
        content = message.photo[-1].file_id
    elif message.video:
        content = message.video.file_id
    else:
        content = message.text or "No content"
    await state.update_data(content=content)
    await message.answer("📦 **Stock amount** (-1 for unlimited):", parse_mode=ParseMode.MARKDOWN)
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
        await message.answer("✅ **Product Added!**", parse_mode=ParseMode.MARKDOWN)
        await state.clear()
    except ValueError:
        await message.answer("❌ **Invalid stock.** Enter an integer.", parse_mode=ParseMode.MARKDOWN)

# Edit/Delete category
@router.callback_query(F.data.startswith("admin_edit_cat_"))
async def admin_edit_cat(callback: CallbackQuery, state: FSMContext):
    cat_id = int(callback.data.split("_")[3])
    await state.update_data(edit_cat_id=cat_id)
    await callback.message.answer("✏️ **Enter new category name:**", parse_mode=ParseMode.MARKDOWN)
    await state.set_state(AdminStates.waiting_for_category_edit)

@router.message(AdminStates.waiting_for_category_edit)
async def admin_update_cat(message: types.Message, state: FSMContext):
    new_name = message.text
    data = await state.get_data()
    cat_id = data['edit_cat_id']
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE categories SET name = ? WHERE id = ?", (new_name, cat_id))
        await db.commit()
    await message.answer("✅ **Category updated.**", parse_mode=ParseMode.MARKDOWN)
    await state.clear()

@router.callback_query(F.data.startswith("admin_del_cat_"))
async def admin_del_cat(callback: CallbackQuery):
    cat_id = int(callback.data.split("_")[3])
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
        await db.commit()
    await callback.answer("🗑️ Category deleted.")
    await callback.message.delete()

# Edit product
@router.callback_query(F.data.startswith("admin_edit_prod_"))
async def admin_edit_prod(callback: CallbackQuery, state: FSMContext):
    prod_id = int(callback.data.split("_")[3])
    await state.update_data(edit_prod_id=prod_id)
    builder = InlineKeyboardBuilder()
    builder.button(text="Name", callback_data="edit_name")
    builder.button(text="Description", callback_data="edit_desc")
    builder.button(text="Price", callback_data="edit_price")
    builder.button(text="Stock", callback_data="edit_stock")
    builder.button(text="Content", callback_data="edit_content")
    builder.button(text="Type", callback_data="edit_type")
    builder.adjust(2)
    await callback.message.answer("✏️ **What do you want to edit?**", reply_markup=builder.as_markup(), parse_mode=ParseMode.MARKDOWN)
    await state.set_state(AdminStates.waiting_for_product_edit_field)

@router.callback_query(AdminStates.waiting_for_product_edit_field)
async def admin_edit_field_chosen(callback: CallbackQuery, state: FSMContext):
    field = callback.data.replace("edit_", "")
    await state.update_data(edit_field=field)
    await callback.message.answer(f"✏️ **Enter new value for {field}:**", parse_mode=ParseMode.MARKDOWN)
    await state.set_state(AdminStates.waiting_for_product_edit_value)

@router.message(AdminStates.waiting_for_product_edit_value)
async def admin_update_product(message: types.Message, state: FSMContext):
    value = message.text
    data = await state.get_data()
    prod_id = data['edit_prod_id']
    field = data['edit_field']
    if field in ['price']:
        try:
            value = float(value)
        except:
            await message.answer("❌ **Invalid number.**", parse_mode=ParseMode.MARKDOWN)
            return
    elif field in ['stock']:
        try:
            value = int(value)
        except:
            await message.answer("❌ **Invalid integer.**", parse_mode=ParseMode.MARKDOWN)
            return
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(f"UPDATE products SET {field} = ? WHERE id = ?", (value, prod_id))
        await db.commit()
    await message.answer("✅ **Product updated.**", parse_mode=ParseMode.MARKDOWN)
    await state.clear()

@router.callback_query(F.data.startswith("admin_del_prod_"))
async def admin_del_prod(callback: CallbackQuery):
    prod_id = int(callback.data.split("_")[3])
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM products WHERE id = ?", (prod_id,))
        await db.commit()
    await callback.answer("🗑️ Product deleted.")
    await callback.message.delete()

# ---------- Users ----------
@router.callback_query(F.data == "admin_users")
async def admin_users_menu(callback: CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.button(text="🔎 Search User", callback_data="admin_search_user")
    builder.button(text="📋 List All Users", callback_data="admin_list_users")
    builder.adjust(1)
    await callback.message.edit_text("👥 **User Management**", reply_markup=builder.as_markup(), parse_mode=ParseMode.MARKDOWN)

@router.callback_query(F.data == "admin_search_user")
async def admin_search_user(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("🆔 **Enter User ID or Username:**", parse_mode=ParseMode.MARKDOWN)
    await state.set_state(AdminStates.waiting_for_user_search)

@router.message(AdminStates.waiting_for_user_search)
async def admin_show_user(message: types.Message, state: FSMContext):
    query = message.text
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        if query.isdigit():
            sql = "SELECT * FROM users WHERE user_id = ?"
            params = (int(query),)
        else:
            sql = "SELECT * FROM users WHERE username = ?"
            params = (query.replace("@", ""),)
        async with db.execute(sql, params) as cursor:
            user = await cursor.fetchone()

    if not user:
        await message.answer("❌ **User not found.**", parse_mode=ParseMode.MARKDOWN)
        await state.clear()
        return

    text = (
        f"👤 **User Details**\n\n"
        f"🆔 ID: `{user['user_id']}`\n"
        f"👤 Name: {user['full_name']}\n"
        f"📧 Username: @{user['username']}\n"
        f"💰 Balance: `{user['balance']}`\n"
        f"🚫 Banned: `{bool(user['banned'])}`\n"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="💰 Give Balance", callback_data=f"admin_give_bal_{user['user_id']}")
    builder.button(text="🚫 Ban/Unban", callback_data=f"admin_ban_{user['user_id']}")
    builder.button(text="📦 Orders", callback_data=f"admin_user_orders_{user['user_id']}")

    await message.answer(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.MARKDOWN)
    await state.clear()

@router.callback_query(F.data.startswith("admin_give_bal_"))
async def admin_give_balance_ask(callback: CallbackQuery, state: FSMContext):
    user_id = int(callback.data.split("_")[3])
    await state.update_data(target_user_id=user_id)
    await callback.message.answer("💰 **Enter amount to add** (negative to subtract):", parse_mode=ParseMode.MARKDOWN)
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
        await message.answer(f"✅ **Balance updated** by `{amount}`.", parse_mode=ParseMode.MARKDOWN)
        await state.clear()
    except ValueError:
        await message.answer("❌ **Invalid amount.**", parse_mode=ParseMode.MARKDOWN)

@router.callback_query(F.data.startswith("admin_ban_"))
async def admin_ban_user(callback: CallbackQuery):
    user_id = int(callback.data.split("_")[2])
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT banned FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            current = row[0] if row else 0
        new = 1 if current == 0 else 0
        await db.execute("UPDATE users SET banned = ? WHERE user_id = ?", (new, user_id))
        await db.commit()
    status = "banned" if new else "unbanned"
    await callback.answer(f"User {status}!")

@router.callback_query(F.data == "admin_list_users")
async def admin_list_users(callback: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT user_id, full_name, balance FROM users LIMIT 20") as cursor:
            users = await cursor.fetchall()
    if not users:
        await callback.message.answer("📭 No users.", parse_mode=ParseMode.MARKDOWN)
        return
    text = "👥 **First 20 Users:**\n\n"
    for u in users:
        text += f"`{u['user_id']}` | {u['full_name']} | `{u['balance']}` Credits\n"
    await callback.message.answer(text, parse_mode=ParseMode.MARKDOWN)

# ---------- Settings ----------
@router.callback_query(F.data == "admin_settings")
async def admin_settings_menu(callback: CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.button(text="Welcome Msg", callback_data="set_welcome")
    builder.button(text="Currency", callback_data="set_currency")
    builder.button(text="Support Link", callback_data="set_support")
    builder.button(text="Rules", callback_data="set_rules")
    builder.button(text="Force Join", callback_data="set_force_join")
    builder.button(text="Captcha", callback_data="set_captcha")
    builder.button(text="Shop Enabled", callback_data="set_shop_enabled")
    builder.button(text="Referral Reward", callback_data="set_ref_reward")
    builder.button(text="Daily Bonus Amount", callback_data="set_daily_bonus_amount")
    builder.button(text="Daily Bonus Enable", callback_data="set_daily_bonus_enabled")
    builder.button(text="Scratch Rewards", callback_data="set_scratch_rewards")
    builder.button(text="Scratch Enable", callback_data="set_scratch_enabled")
    builder.adjust(2)
    await callback.message.edit_text("⚙️ **Settings**", reply_markup=builder.as_markup(), parse_mode=ParseMode.MARKDOWN)

@router.callback_query(F.data.startswith("set_"))
async def admin_setting_ask(callback: CallbackQuery, state: FSMContext):
    key_map = {
        "set_welcome": "welcome_message",
        "set_currency": "currency",
        "set_support": "support_link",
        "set_rules": "rules",
        "set_force_join": "channel_force_join",
        "set_captcha": "captcha_enabled",
        "set_shop_enabled": "shop_enabled",
        "set_ref_reward": "referral_reward",
        "set_daily_bonus_amount": "daily_bonus_amount",
        "set_daily_bonus_enabled": "daily_bonus_enabled",
        "set_scratch_rewards": "scratch_rewards",
        "set_scratch_enabled": "scratch_enabled",
    }
    key = key_map.get(callback.data)
    if not key:
        await callback.answer("❌ Unknown setting")
        return
    await state.update_data(setting_key=key)
    await callback.message.answer(f"✏️ **Enter new value for `{key}`:**", parse_mode=ParseMode.MARKDOWN)
    await state.set_state(AdminStates.waiting_for_setting_value)

@router.message(AdminStates.waiting_for_setting_value)
async def admin_setting_save(message: types.Message, state: FSMContext):
    value = message.text
    data = await state.get_data()
    key = data['setting_key']
    await update_setting(key, value)
    await message.answer(f"✅ **`{key}` updated.**", parse_mode=ParseMode.MARKDOWN)
    await state.clear()

# ---------- Promos ----------
@router.callback_query(F.data == "admin_promos")
async def admin_promos_menu(callback: CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Create Promo", callback_data="admin_create_promo")
    builder.button(text="📋 List Promos", callback_data="admin_list_promos")
    builder.adjust(1)
    await callback.message.edit_text("🎁 **Promo Codes Management**", reply_markup=builder.as_markup(), parse_mode=ParseMode.MARKDOWN)

@router.callback_query(F.data == "admin_create_promo")
async def admin_create_promo(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("📝 **Enter Promo Code:**", parse_mode=ParseMode.MARKDOWN)
    await state.set_state(AdminStates.waiting_for_promo_code)

@router.message(AdminStates.waiting_for_promo_code)
async def admin_promo_code(message: types.Message, state: FSMContext):
    await state.update_data(code=message.text)
    await message.answer("💰 **Reward Amount:**", parse_mode=ParseMode.MARKDOWN)
    await state.set_state(AdminStates.waiting_for_promo_reward)

@router.message(AdminStates.waiting_for_promo_reward)
async def admin_promo_reward(message: types.Message, state: FSMContext):
    try:
        reward = float(message.text)
        await state.update_data(reward=reward)
        await message.answer("🔢 **Max Usage** (-1 for unlimited):", parse_mode=ParseMode.MARKDOWN)
        await state.set_state(AdminStates.waiting_for_promo_limit)
    except ValueError:
        await message.answer("❌ **Invalid number.**", parse_mode=ParseMode.MARKDOWN)

@router.message(AdminStates.waiting_for_promo_limit)
async def admin_promo_limit(message: types.Message, state: FSMContext):
    try:
        limit = int(message.text)
        data = await state.get_data()
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("INSERT OR REPLACE INTO promos (code, reward, max_usage, expiry_date) VALUES (?, ?, ?, ?)",
                            (data['code'], data['reward'], limit, "2099-01-01"))
            await db.commit()
        await message.answer(f"✅ **Promo '{data['code']}' Created!**", parse_mode=ParseMode.MARKDOWN)
        await state.clear()
    except ValueError:
        await message.answer("❌ **Invalid number.**", parse_mode=ParseMode.MARKDOWN)

@router.callback_query(F.data == "admin_list_promos")
async def admin_list_promos(callback: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM promos") as cursor:
            promos = await cursor.fetchall()
    if not promos:
        await callback.message.answer("📭 No promos.", parse_mode=ParseMode.MARKDOWN)
        return
    for p in promos:
        text = f"Code: `{p['code']}`\nReward: `{p['reward']}`\nUsed: `{p['used_count']}/{p['max_usage']}`\nExpiry: `{p['expiry_date']}`"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑️ Delete", callback_data=f"admin_del_promo_{p['code']}")]
        ])
        await callback.message.answer(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

@router.callback_query(F.data.startswith("admin_del_promo_"))
async def admin_del_promo(callback: CallbackQuery):
    code = callback.data.split("_")[3]
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM promos WHERE code = ?", (code,))
        await db.commit()
    await callback.answer("🗑️ Promo deleted.")
    await callback.message.delete()

# ---------- Tasks ----------
@router.callback_query(F.data == "admin_tasks")
async def admin_tasks_menu(callback: CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Create Task", callback_data="admin_create_task")
    builder.button(text="📋 List Tasks", callback_data="admin_list_tasks")
    builder.adjust(1)
    await callback.message.edit_text("📋 **Task Management**", reply_markup=builder.as_markup(), parse_mode=ParseMode.MARKDOWN)

@router.callback_query(F.data == "admin_create_task")
async def admin_create_task(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("📝 **Task Description:**", parse_mode=ParseMode.MARKDOWN)
    await state.set_state(AdminStates.waiting_for_task_desc)

@router.message(AdminStates.waiting_for_task_desc)
async def admin_task_desc(message: types.Message, state: FSMContext):
    await state.update_data(desc=message.text)
    await message.answer("🔗 **Task Link** (or 'None'):", parse_mode=ParseMode.MARKDOWN)
    await state.set_state(AdminStates.waiting_for_task_link)

@router.message(AdminStates.waiting_for_task_link)
async def admin_task_link(message: types.Message, state: FSMContext):
    await state.update_data(link=message.text)
    await message.answer("💰 **Reward Amount:**", parse_mode=ParseMode.MARKDOWN)
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
        await message.answer("✅ **Task Created!**", parse_mode=ParseMode.MARKDOWN)
        await state.clear()
    except ValueError:
        await message.answer("❌ **Invalid number.**", parse_mode=ParseMode.MARKDOWN)

@router.callback_query(F.data == "admin_list_tasks")
async def admin_list_tasks(callback: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM tasks") as cursor:
            tasks = await cursor.fetchall()
    if not tasks:
        await callback.message.answer("📭 No tasks.", parse_mode=ParseMode.MARKDOWN)
        return
    for t in tasks:
        text = f"ID: `{t['id']}`\nDesc: {t['description']}\nLink: {t['link']}\nReward: `{t['reward']}`"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Edit", callback_data=f"admin_edit_task_{t['id']}"),
             InlineKeyboardButton(text="🗑️ Delete", callback_data=f"admin_del_task_{t['id']}")]
        ])
        await callback.message.answer(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

@router.callback_query(F.data.startswith("admin_del_task_"))
async def admin_del_task(callback: CallbackQuery):
    task_id = int(callback.data.split("_")[3])
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        await db.commit()
    await callback.answer("🗑️ Task deleted.")
    await callback.message.delete()

# ---------- Orders ----------
@router.callback_query(F.data == "admin_orders")
async def admin_orders(callback: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT orders.*, products.name 
            FROM orders 
            JOIN products ON orders.product_id = products.id 
            WHERE status='pending'
        """) as cursor:
            orders = await cursor.fetchall()
    if not orders:
        await callback.message.answer("📭 No pending orders.", parse_mode=ParseMode.MARKDOWN)
        return
    for order in orders:
        text = f"📦 **Order #{order['id']}**\nProduct: {order['name']}\nUser: `{order['user_id']}`\nData: {order['data']}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Deliver", callback_data=f"admin_deliver_order_{order['id']}")]
        ])
        await callback.message.answer(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

@router.callback_query(F.data.startswith("admin_deliver_order_"))
async def admin_deliver_order(callback: CallbackQuery, bot: Bot):
    order_id = int(callback.data.split("_")[4])
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE orders SET status='delivered' WHERE id=?", (order_id,))
        await db.commit()
    await callback.message.edit_text(f"✅ **Order #{order_id} marked as delivered.**", parse_mode=ParseMode.MARKDOWN)

# ---------- Main ----------
async def main():
    if not TOKEN:
        print("❌ Bot token not found. Please set TELEGRAM_BOT_TOKEN and ADMIN_ID in environment.")
        return

    await init_db()

    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    print(f"🤖 Bot is running on port {PORT}...")
    
    # Start Flask in a separate thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # Start bot polling
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot)
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("👋 Bot stopped")
