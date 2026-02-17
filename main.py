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
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters import Command, CommandStart
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    CallbackQuery,
    Message,
    ParseMode,
    ContentType,
)
from aiogram.utils.callback_data import CallbackData
from aiogram.utils.exceptions import ChatNotFound
from dotenv import load_dotenv

# Load environment
load_dotenv()

# ---------- Configuration ----------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")
DB_NAME = "shop_bot.db"
PORT = int(os.getenv("PORT", 10000))

# ---------- Flask Health Check ----------
app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({
        "status": "running",
        "bot": "Telegram Shop Bot",
        "version": "3.0",
        "timestamp": datetime.now().isoformat()
    })

@app.route('/health')
def health():
    return jsonify({"status": "healthy"}), 200

def run_flask():
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)

# ---------- Database Setup ----------
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
            "rules": "📜 **Rules:**\n1. No spamming\n2. Be respectful\n3. Enjoy!",
            "channel_force_join": "",
            "captcha_enabled": "1",
            "shop_enabled": "1",
            "referral_reward": "5.0",
            "daily_bonus_enabled": "1",
            "daily_bonus_amount": "10",
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
            """, (referrer_id, reward, f"Referral from {full_name}"))
        
        await db.commit()

# ---------- Backup Function ----------
async def create_backup(bot: Bot, chat_id: int):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_filename = f"backup_{timestamp}.zip"
    
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        if os.path.exists(DB_NAME):
            zip_file.write(DB_NAME, arcname=DB_NAME)
        
        readme = f"""Backup: {datetime.now()}
DB: {DB_NAME}
Admin: {ADMIN_ID}
"""
        zip_file.writestr("README.txt", readme)
    
    zip_buffer.seek(0)
    
    await bot.send_document(
        chat_id,
        types.InputFile(zip_buffer, filename=backup_filename),
        caption=f"📦 **Database Backup**\n{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )

# ---------- FSM States ----------
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
    waiting_for_product_edit_field = State()
    waiting_for_product_edit_value = State()
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

class ShopStates(StatesGroup):
    captcha = State()
    waiting_for_address = State()

class UserStates(StatesGroup):
    waiting_for_promo = State()

# ---------- Emoji Captcha ----------
EMOJI_LIST = ["😀", "😂", "😍", "🥺", "😎", "🎉", "🔥", "⭐", "🐶", "🐱", "🐼"]

def generate_emoji_captcha():
    seq = random.sample(EMOJI_LIST, 4)
    missing = random.randint(0, 3)
    answer = seq[missing]
    seq[missing] = "___"
    return " ".join(seq), answer

# ---------- Keyboards ----------
def main_menu_kb(is_admin=False):
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🛍 Shop", "👤 Profile", "🎁 Daily Bonus")
    kb.row("🎲 Scratch Card", "📋 Tasks", "ℹ️ Support")
    kb.row("📜 Rules")
    if is_admin:
        kb.row("⚙️ Admin Panel")
    return kb

def admin_panel_kb():
    kb = InlineKeyboardMarkup(row_width=2)
    buttons = [
        InlineKeyboardButton("📊 Stats", callback_data="admin_stats"),
        InlineKeyboardButton("👥 Users", callback_data="admin_users"),
        InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"),
        InlineKeyboardButton("🛍 Shop", callback_data="admin_shop"),
        InlineKeyboardButton("⚙️ Settings", callback_data="admin_settings"),
        InlineKeyboardButton("🎁 Promos", callback_data="admin_promos"),
        InlineKeyboardButton("📋 Tasks", callback_data="admin_tasks"),
        InlineKeyboardButton("📦 Orders", callback_data="admin_orders"),
        InlineKeyboardButton("📦 Backup DB", callback_data="admin_backup"),
    ]
    kb.add(*buttons)
    return kb

def shop_mgmt_kb():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("➕ Add Category", callback_data="admin_add_cat"))
    kb.add(InlineKeyboardButton("📋 List Categories", callback_data="admin_list_cats"))
    kb.add(InlineKeyboardButton("🔙 Back", callback_data="admin_panel"))
    return kb

# ---------- Bot Initialization ----------
bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

# ---------- Start & Captcha ----------
@dp.message_handler(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    args = message.get_args()
    referrer_id = None
    if args and args.isdigit():
        ref = int(args)
        if ref != message.from_user.id and await get_user(ref):
            referrer_id = ref

    await add_user(message.from_user.id, message.from_user.username, 
                   message.from_user.full_name, referrer_id)

    # Force join check
    channel = await get_setting("channel_force_join")
    if channel:
        try:
            member = await bot.get_chat_member(channel, message.from_user.id)
            if member.status == "left":
                await message.reply(f"🚫 Join our channel first:\n{channel}")
                return
        except:
            pass

    if await get_setting("captcha_enabled") == "1":
        q, a = generate_emoji_captcha()
        await state.update_data(captcha_answer=a)
        await ShopStates.captcha.set()
        await message.reply(f"🔒 **Captcha:**\n`{q}`\n\nType missing emoji:", parse_mode=ParseMode.MARKDOWN)
    else:
        await show_welcome(message)

@dp.message_handler(state=ShopStates.captcha)
async def process_captcha(message: Message, state: FSMContext):
    data = await state.get_data()
    if message.text.strip() == data.get("captcha_answer"):
        await state.finish()
        await show_welcome(message)
    else:
        await message.reply("❌ Wrong. Try again.")
        q, a = generate_emoji_captcha()
        await state.update_data(captcha_answer=a)
        await message.reply(f"New:\n`{q}`\n\nType missing:", parse_mode=ParseMode.MARKDOWN)

async def show_welcome(message: Message):
    welcome = await get_setting("welcome_message")
    user = await get_user(message.from_user.id)
    if user and user['banned']:
        await message.reply("🚫 You are banned.")
        return
    welcome = welcome.replace("{name}", message.from_user.full_name)
    is_admin = str(message.from_user.id) == ADMIN_ID
    await message.reply(welcome, reply_markup=main_menu_kb(is_admin), parse_mode=ParseMode.MARKDOWN)

# ---------- Profile ----------
@dp.message_handler(lambda msg: msg.text == "👤 Profile")
async def profile(message: Message):
    user = await get_user(message.from_user.id)
    if not user:
        return await message.reply("User not found. Use /start")
    
    spent = user['total_spent']
    if spent < 100: level = "🥉 Bronze"
    elif spent < 500: level = "🥈 Silver"
    else: level = "🥇 Gold"
    
    text = f"""👤 **Profile**
ID: `{user['user_id']}`
💰 Balance: `{user['balance']}` {await get_setting('currency')}
📊 Spent: `{spent}` Credits
🏅 Level: {level}
📅 Joined: {user['joined_at']}"""
    
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("📦 Orders", callback_data="my_orders"))
    kb.add(InlineKeyboardButton("📜 History", callback_data="my_transactions"))
    kb.add(InlineKeyboardButton("🎁 Redeem", callback_data="redeem_promo"))
    
    await message.reply(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    
    bot_info = await bot.me
    ref_link = f"https://t.me/{bot_info.username}?start={user['user_id']}"
    await message.reply(f"🔗 **Referral Link:**\n`{ref_link}`")

# ---------- Daily Bonus ----------
@dp.message_handler(lambda msg: msg.text == "🎁 Daily Bonus")
async def daily_bonus(message: Message):
    if await get_setting("daily_bonus_enabled") != "1":
        return await message.reply("❌ Daily bonus disabled")
    
    user_id = message.from_user.id
    today = date.today().isoformat()
    
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT last_claim FROM daily_bonus WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        if row and row[0] == today:
            return await message.reply("⏳ Already claimed today")
        
        amount = float(await get_setting("daily_bonus_amount"))
        await db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, user_id))
        await db.execute("INSERT OR REPLACE INTO daily_bonus VALUES (?,?)", (user_id, today))
        await db.execute("INSERT INTO transactions (user_id,amount,type,description) VALUES (?,?,'daily_bonus','Daily bonus')", (user_id, amount))
        await db.commit()
    
    await message.reply(f"🎉 **+{amount} Credits**")

# ---------- Scratch Card ----------
@dp.message_handler(lambda msg: msg.text == "🎲 Scratch Card")
async def scratch_card(message: Message):
    if await get_setting("scratch_enabled") != "1":
        return await message.reply("❌ Scratch disabled")
    
    user_id = message.from_user.id
    today = date.today().isoformat()
    
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT last_scratch FROM daily_scratch WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        if row and row[0] == today:
            return await message.reply("⏳ Already scratched today")
        
        rewards = [float(x) for x in (await get_setting("scratch_rewards")).split(",") if x]
        amount = random.choice(rewards) if rewards else 10
        
        await db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, user_id))
        await db.execute("INSERT OR REPLACE INTO daily_scratch VALUES (?,?)", (user_id, today))
        await db.execute("INSERT INTO transactions (user_id,amount,type,description) VALUES (?,?,'scratch','Scratch card')", (user_id, amount))
        await db.commit()
    
    await message.reply(f"🎲 **You won {amount} Credits!**")

# ---------- Tasks ----------
@dp.message_handler(lambda msg: msg.text == "📋 Tasks")
async def tasks_list(message: Message):
    user_id = message.from_user.id
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT * FROM tasks 
            WHERE id NOT IN (SELECT task_id FROM completed_tasks WHERE user_id=?)
        """, (user_id,))
        tasks = await cur.fetchall()
    
    if not tasks:
        return await message.reply("✅ No tasks available")
    
    text = "📋 **Tasks:**\n"
    kb = InlineKeyboardMarkup()
    for t in tasks:
        text += f"\n🔹 {t['description']} – {t['reward']} Credits"
        kb.add(InlineKeyboardButton(f"✅ Do Task", callback_data=f"do_task_{t['id']}"))
    
    await message.reply(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

@dp.callback_query_handler(lambda c: c.data.startswith("do_task_"))
async def do_task(callback: CallbackQuery):
    task_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id
    
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM tasks WHERE id=?", (task_id,))
        task = await cur.fetchone()
        if not task:
            return await callback.answer("Task not found")
        
        cur = await db.execute("SELECT * FROM completed_tasks WHERE user_id=? AND task_id=?", (user_id, task_id))
        if await cur.fetchone():
            return await callback.answer("Already done")
        
        await db.execute("INSERT INTO completed_tasks VALUES (?,?)", (user_id, task_id))
        await db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (task['reward'], user_id))
        await db.execute("INSERT INTO transactions (user_id,amount,type,description) VALUES (?,?,'task',?)", 
                        (user_id, task['reward'], task['description']))
        await db.commit()
    
    await callback.message.edit_text(f"✅ **Task done!** +{task['reward']} Credits")
    await callback.answer()

# ---------- Promo ----------
@dp.callback_query_handler(lambda c: c.data == "redeem_promo")
async def promo_ask(callback: CallbackQuery, state: FSMContext):
    await callback.message.reply("🎁 **Enter promo code:**")
    await UserStates.waiting_for_promo.set()
    await callback.answer()

@dp.message_handler(state=UserStates.waiting_for_promo)
async def promo_redeem(message: Message, state: FSMContext):
    code = message.text.strip()
    user_id = message.from_user.id
    
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM promos WHERE code=?", (code,))
        promo = await cur.fetchone()
        
        if not promo:
            return await message.reply("❌ Invalid code")
        if promo['expiry_date'] and promo['expiry_date'] < datetime.now().strftime("%Y-%m-%d"):
            return await message.reply("❌ Expired")
        if promo['max_usage'] != -1 and promo['used_count'] >= promo['max_usage']:
            return await message.reply("❌ Limit reached")
        
        cur = await db.execute("SELECT * FROM promo_usage WHERE user_id=? AND code=?", (user_id, code))
        if await cur.fetchone():
            return await message.reply("❌ Already used")
        
        await db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (promo['reward'], user_id))
        await db.execute("UPDATE promos SET used_count=used_count+1 WHERE code=?", (code,))
        await db.execute("INSERT INTO promo_usage VALUES (?,?)", (user_id, code))
        await db.execute("INSERT INTO transactions (user_id,amount,type,description) VALUES (?,?,'promo',?)", 
                        (user_id, promo['reward'], f"Promo {code}"))
        await db.commit()
    
    await message.reply(f"✅ **+{promo['reward']} Credits**")
    await state.finish()

# ---------- Shop ----------
@dp.message_handler(lambda msg: msg.text == "🛍 Shop")
async def shop(message: Message):
    if await get_setting("shop_enabled") != "1":
        return await message.reply("⚠️ Shop disabled")
    
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM categories")
        cats = await cur.fetchall()
    
    if not cats:
        return await message.reply("📭 No categories")
    
    kb = InlineKeyboardMarkup(row_width=2)
    for c in cats:
        kb.insert(InlineKeyboardButton(c['name'], callback_data=f"cat_{c['id']}"))
    
    await message.reply("📂 **Categories:**", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data.startswith("cat_"))
async def show_products(callback: CallbackQuery):
    cat_id = int(callback.data.split("_")[1])
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM products WHERE category_id=?", (cat_id,))
        prods = await cur.fetchall()
    
    if not prods:
        return await callback.message.edit_text("📭 No products")
    
    kb = InlineKeyboardMarkup()
    for p in prods:
        stock = "∞" if p['stock'] == -1 else p['stock']
        kb.add(InlineKeyboardButton(f"{p['name']} | {p['price']} | Stock:{stock}", callback_data=f"prod_{p['id']}"))
    
    await callback.message.edit_text("📦 **Products:**", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data.startswith("prod_"))
async def product_detail(callback: CallbackQuery):
    prod_id = int(callback.data.split("_")[1])
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM products WHERE id=?", (prod_id,))
        prod = await cur.fetchone()
    
    if not prod:
        return await callback.answer("Not found")
    
    text = f"""📦 **{prod['name']}**
{prod['description']}
💰 Price: `{prod['price']}` {await get_setting('currency')}
📦 Stock: `{'Unlimited' if prod['stock']==-1 else prod['stock']}`
Type: `{prod['type']}`"""
    
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("💳 Buy", callback_data=f"buy_{prod['id']}"))
    
    await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

@dp.callback_query_handler(lambda c: c.data.startswith("buy_"))
async def buy(callback: CallbackQuery, state: FSMContext):
    prod_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM products WHERE id=?", (prod_id,))
        prod = await cur.fetchone()
        cur = await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        user = await cur.fetchone()
    
    if not prod or not user:
        return await callback.answer("Error")
    if prod['stock'] != -1 and prod['stock'] <= 0:
        return await callback.answer("Out of stock")
    if user['balance'] < prod['price']:
        return await callback.answer("Insufficient balance")
    
    if prod['type'] == 'physical':
        await state.update_data(buy_prod_id=prod_id)
        await callback.message.reply("📦 **Enter shipping address:**")
        await ShopStates.waiting_for_address.set()
        await callback.answer()
        return
    
    await process_purchase(user_id, prod, callback.message)

async def process_purchase(user_id, prod, msg, address=""):
    new_balance = user['balance'] - prod['price']
    new_stock = prod['stock'] - 1 if prod['stock'] != -1 else -1
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET balance=?, total_spent=total_spent+? WHERE user_id=?", 
                        (new_balance, prod['price'], user_id))
        if new_stock != -1:
            await db.execute("UPDATE products SET stock=? WHERE id=?", (new_stock, prod['id']))
        
        status = "delivered" if prod['type'] in ('digital','file') else "pending"
        data = prod['content'] if status == "delivered" else address
        await db.execute("INSERT INTO orders (user_id,product_id,status,data) VALUES (?,?,?,?)",
                        (user_id, prod['id'], status, data))
        await db.execute("INSERT INTO transactions (user_id,amount,type,description) VALUES (?,?,'purchase',?)",
                        (user_id, -prod['price'], f"Bought {prod['name']}"))
        await db.commit()
    
    await msg.answer(f"✅ **Purchase done!** Balance: {new_balance} Credits")
    
    if prod['type'] == 'file':
        try:
            await bot.send_document(user_id, prod['content'], caption=prod['name'])
        except:
            await msg.answer("❌ File delivery failed")
    elif prod['type'] == 'digital':
        await msg.answer(f"📦 **Your item:**\n`{prod['content']}`", parse_mode=ParseMode.MARKDOWN)

@dp.message_handler(state=ShopStates.waiting_for_address)
async def save_address(message: Message, state: FSMContext):
    addr = message.text
    data = await state.get_data()
    prod_id = data['buy_prod_id']
    
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM products WHERE id=?", (prod_id,))
        prod = await cur.fetchone()
        cur = await db.execute("SELECT * FROM users WHERE user_id=?", (message.from_user.id,))
        user = await cur.fetchone()
    
    await process_purchase(message.from_user.id, prod, message, addr)
    await state.finish()

@dp.callback_query_handler(lambda c: c.data == "my_orders")
async def my_orders(callback: CallbackQuery):
    user_id = callback.from_user.id
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT orders.*, products.name FROM orders 
            JOIN products ON orders.product_id=products.id 
            WHERE user_id=? ORDER BY created_at DESC LIMIT 5
        """, (user_id,))
        orders = await cur.fetchall()
    
    if not orders:
        return await callback.message.edit_text("📭 No orders")
    
    text = "📦 **Recent Orders:**\n"
    for o in orders:
        text += f"\n🔹 {o['name']} – {o['status'].upper()}"
    
    await callback.message.edit_text(text, parse_mode=ParseMode.MARKDOWN)

# ---------- Admin Panel ----------
@dp.message_handler(lambda msg: msg.text == "⚙️ Admin Panel")
async def admin_panel(message: Message):
    if str(message.from_user.id) != ADMIN_ID:
        return
    await message.reply("🔧 **Admin Panel**", reply_markup=admin_panel_kb())

@dp.callback_query_handler(lambda c: c.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        users = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM orders")
        orders = (await cur.fetchone())[0]
        cur = await db.execute("SELECT SUM(price) FROM products JOIN orders ON products.id=orders.product_id")
        revenue = (await cur.fetchone())[0] or 0
    
    text = f"""📊 **Stats**
👥 Users: `{users}`
📦 Orders: `{orders}`
💰 Revenue: `{revenue}` {await get_setting('currency')}"""
    
    await callback.message.edit_text(text, reply_markup=admin_panel_kb(), parse_mode=ParseMode.MARKDOWN)

@dp.callback_query_handler(lambda c: c.data == "admin_backup")
async def admin_backup(callback: CallbackQuery):
    await callback.message.edit_text("📦 Creating backup...")
    await create_backup(bot, callback.from_user.id)
    await callback.answer()

@dp.callback_query_handler(lambda c: c.data == "admin_broadcast")
async def broadcast_ask(callback: CallbackQuery, state: FSMContext):
    await callback.message.reply("📝 **Send broadcast message:**")
    await AdminStates.waiting_for_broadcast.set()
    await callback.answer()

@dp.message_handler(state=AdminStates.waiting_for_broadcast, content_types=ContentType.ANY)
async def broadcast_send(message: Message, state: FSMContext):
    await state.finish()
    msg = await message.reply("⏳ Broadcasting...")
    
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT user_id FROM users")
        users = await cur.fetchall()
    
    count = 0
    for u in users:
        try:
            await message.copy_to(u[0])
            count += 1
            if count % 10 == 0:
                await asyncio.sleep(0.3)
        except:
            pass
    
    await msg.edit_text(f"✅ Sent to {count} users")

@dp.callback_query_handler(lambda c: c.data == "admin_shop")
async def admin_shop_menu(callback: CallbackQuery):
    await callback.message.edit_text("🛍 **Shop Management**", reply_markup=shop_mgmt_kb())

@dp.callback_query_handler(lambda c: c.data == "admin_add_cat")
async def add_cat(callback: CallbackQuery, state: FSMContext):
    await callback.message.reply("📝 **Category name:**")
    await AdminStates.waiting_for_category_name.set()
    await callback.answer()

@dp.message_handler(state=AdminStates.waiting_for_category_name)
async def save_cat(message: Message, state: FSMContext):
    name = message.text
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (name,))
        await db.commit()
    await message.reply(f"✅ Category '{name}' added")
    await state.finish()

@dp.callback_query_handler(lambda c: c.data == "admin_list_cats")
async def list_cats(callback: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM categories")
        cats = await cur.fetchall()
    
    if not cats:
        return await callback.message.edit_text("📭 No categories")
    
    kb = InlineKeyboardMarkup()
    for c in cats:
        kb.row(
            InlineKeyboardButton(f"📁 {c['name']}", callback_data=f"admin_cat_{c['id']}"),
            InlineKeyboardButton("✏️", callback_data=f"admin_edit_cat_{c['id']}"),
            InlineKeyboardButton("🗑️", callback_data=f"admin_del_cat_{c['id']}")
        )
    kb.add(InlineKeyboardButton("🔙 Back", callback_data="admin_shop"))
    
    await callback.message.edit_text("📂 **Categories:**", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data.startswith("admin_edit_cat_"))
async def edit_cat(callback: CallbackQuery, state: FSMContext):
    cat_id = int(callback.data.split("_")[3])
    await state.update_data(edit_cat_id=cat_id)
    await callback.message.reply("✏️ **New name:**")
    await AdminStates.waiting_for_category_edit.set()
    await callback.answer()

@dp.message_handler(state=AdminStates.waiting_for_category_edit)
async def update_cat(message: Message, state: FSMContext):
    new_name = message.text
    data = await state.get_data()
    cat_id = data['edit_cat_id']
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE categories SET name=? WHERE id=?", (new_name, cat_id))
        await db.commit()
    await message.reply("✅ Category updated")
    await state.finish()

@dp.callback_query_handler(lambda c: c.data.startswith("admin_del_cat_"))
async def del_cat(callback: CallbackQuery):
    cat_id = int(callback.data.split("_")[3])
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM categories WHERE id=?", (cat_id,))
        await db.commit()
    await callback.answer("🗑️ Deleted")
    await callback.message.delete()

@dp.callback_query_handler(lambda c: c.data.startswith("admin_cat_"))
async def cat_products(callback: CallbackQuery):
    cat_id = int(callback.data.split("_")[2])
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM products WHERE category_id=?", (cat_id,))
        prods = await cur.fetchall()
    
    kb = InlineKeyboardMarkup()
    for p in prods:
        kb.row(
            InlineKeyboardButton(f"{p['name']} | {p['price']}", callback_data=f"admin_prod_{p['id']}"),
            InlineKeyboardButton("✏️", callback_data=f"admin_edit_prod_{p['id']}"),
            InlineKeyboardButton("🗑️", callback_data=f"admin_del_prod_{p['id']}")
        )
    kb.add(InlineKeyboardButton("➕ Add Product", callback_data=f"admin_add_prod_cat_{cat_id}"))
    kb.add(InlineKeyboardButton("🔙 Back", callback_data="admin_list_cats"))
    
    await callback.message.edit_text("📦 **Products:**", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data.startswith("admin_add_prod_cat_"))
async def add_prod_start(callback: CallbackQuery, state: FSMContext):
    cat_id = int(callback.data.split("_")[4])
    await state.update_data(cat_id=cat_id)
    await callback.message.reply("📝 **Product name:**")
    await AdminStates.waiting_for_product_name.set()
    await callback.answer()

@dp.message_handler(state=AdminStates.waiting_for_product_name)
async def prod_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.reply("📝 **Description:**")
    await AdminStates.waiting_for_product_desc.set()

@dp.message_handler(state=AdminStates.waiting_for_product_desc)
async def prod_desc(message: Message, state: FSMContext):
    await state.update_data(desc=message.text)
    await message.reply("💰 **Price:**")
    await AdminStates.waiting_for_product_price.set()

@dp.message_handler(state=AdminStates.waiting_for_product_price)
async def prod_price(message: Message, state: FSMContext):
    try:
        price = float(message.text)
        await state.update_data(price=price)
        
        kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        kb.row("digital", "file", "physical")
        
        await message.reply("📦 **Type:**", reply_markup=kb)
        await AdminStates.waiting_for_product_type.set()
    except:
        await message.reply("❌ Invalid price")

@dp.message_handler(state=AdminStates.waiting_for_product_type)
async def prod_type(message: Message, state: FSMContext):
    ptype = message.text.lower()
    if ptype not in ('digital','file','physical'):
        return await message.reply("❌ Invalid type")
    
    await state.update_data(type=ptype)
    
    if ptype == 'physical':
        await state.update_data(content="Physical item")
        await message.reply("📦 **Stock** (-1 unlimited):", reply_markup=types.ReplyKeyboardRemove())
        await AdminStates.waiting_for_product_stock.set()
    else:
        await message.reply("📄 **Content** (text or upload file):", reply_markup=types.ReplyKeyboardRemove())
        await AdminStates.waiting_for_product_content.set()

@dp.message_handler(state=AdminStates.waiting_for_product_content, content_types=['text','document','photo','video'])
async def prod_content(message: Message, state: FSMContext):
    if message.document:
        content = message.document.file_id
    elif message.photo:
        content = message.photo[-1].file_id
    elif message.video:
        content = message.video.file_id
    else:
        content = message.text or "No content"
    
    await state.update_data(content=content)
    await message.reply("📦 **Stock** (-1 unlimited):")
    await AdminStates.waiting_for_product_stock.set()

@dp.message_handler(state=AdminStates.waiting_for_product_stock)
async def prod_stock(message: Message, state: FSMContext):
    try:
        stock = int(message.text)
        data = await state.get_data()
        
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("""
                INSERT INTO products (category_id, name, description, price, type, content, stock)
                VALUES (?,?,?,?,?,?,?)
            """, (data['cat_id'], data['name'], data['desc'], data['price'], data['type'], data['content'], stock))
            await db.commit()
        
        await message.reply("✅ **Product added!**")
        await state.finish()
    except:
        await message.reply("❌ Invalid stock")

@dp.callback_query_handler(lambda c: c.data.startswith("admin_edit_prod_"))
async def edit_prod(callback: CallbackQuery, state: FSMContext):
    prod_id = int(callback.data.split("_")[3])
    await state.update_data(edit_prod_id=prod_id)
    
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("Name", callback_data="edit_name"),
        InlineKeyboardButton("Description", callback_data="edit_desc"),
        InlineKeyboardButton("Price", callback_data="edit_price"),
        InlineKeyboardButton("Stock", callback_data="edit_stock"),
        InlineKeyboardButton("Content", callback_data="edit_content"),
        InlineKeyboardButton("Type", callback_data="edit_type"),
    )
    
    await callback.message.reply("✏️ **Edit field:**", reply_markup=kb)
    await AdminStates.waiting_for_product_edit_field.set()
    await callback.answer()

@dp.callback_query_handler(state=AdminStates.waiting_for_product_edit_field)
async def edit_field_chosen(callback: CallbackQuery, state: FSMContext):
    field = callback.data.replace("edit_", "")
    await state.update_data(edit_field=field)
    await callback.message.reply(f"✏️ **New value for {field}:**")
    await AdminStates.waiting_for_product_edit_value.set()
    await callback.answer()

@dp.message_handler(state=AdminStates.waiting_for_product_edit_value)
async def update_product(message: Message, state: FSMContext):
    value = message.text
    data = await state.get_data()
    prod_id = data['edit_prod_id']
    field = data['edit_field']
    
    if field in ('price', 'stock'):
        try:
            value = float(value) if field=='price' else int(value)
        except:
            return await message.reply("❌ Invalid number")
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(f"UPDATE products SET {field}=? WHERE id=?", (value, prod_id))
        await db.commit()
    
    await message.reply("✅ Product updated")
    await state.finish()

@dp.callback_query_handler(lambda c: c.data.startswith("admin_del_prod_"))
async def del_prod(callback: CallbackQuery):
    prod_id = int(callback.data.split("_")[3])
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM products WHERE id=?", (prod_id,))
        await db.commit()
    await callback.answer("🗑️ Deleted")
    await callback.message.delete()

@dp.callback_query_handler(lambda c: c.data == "admin_users")
async def users_menu(callback: CallbackQuery):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🔎 Search", callback_data="admin_search_user"))
    kb.add(InlineKeyboardButton("📋 List", callback_data="admin_list_users"))
    await callback.message.edit_text("👥 **Users**", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data == "admin_search_user")
async def search_user(callback: CallbackQuery, state: FSMContext):
    await callback.message.reply("🆔 **User ID or username:**")
    await AdminStates.waiting_for_user_search.set()
    await callback.answer()

@dp.message_handler(state=AdminStates.waiting_for_user_search)
async def show_user(message: Message, state: FSMContext):
    query = message.text
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        if query.isdigit():
            cur = await db.execute("SELECT * FROM users WHERE user_id=?", (int(query),))
        else:
            cur = await db.execute("SELECT * FROM users WHERE username=?", (query.replace('@',''),))
        user = await cur.fetchone()
    
    if not user:
        return await message.reply("❌ Not found")
    
    text = f"""👤 **User**
ID: `{user['user_id']}`
Name: {user['full_name']}
Username: @{user['username']}
Balance: `{user['balance']}`
Banned: `{bool(user['banned'])}`"""
    
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("💰 Give", callback_data=f"admin_give_bal_{user['user_id']}"))
    kb.add(InlineKeyboardButton("🚫 Ban", callback_data=f"admin_ban_{user['user_id']}"))
    
    await message.reply(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    await state.finish()

@dp.callback_query_handler(lambda c: c.data.startswith("admin_give_bal_"))
async def give_balance(callback: CallbackQuery, state: FSMContext):
    user_id = int(callback.data.split("_")[3])
    await state.update_data(target_user_id=user_id)
    await callback.message.reply("💰 **Amount (+/-):**")
    await AdminStates.waiting_for_balance_change.set()
    await callback.answer()

@dp.message_handler(state=AdminStates.waiting_for_balance_change)
async def update_balance(message: Message, state: FSMContext):
    try:
        amount = float(message.text)
        data = await state.get_data()
        user_id = data['target_user_id']
        
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, user_id))
            await db.commit()
        
        await message.reply(f"✅ Balance updated by {amount}")
        await state.finish()
    except:
        await message.reply("❌ Invalid amount")

@dp.callback_query_handler(lambda c: c.data.startswith("admin_ban_"))
async def ban_user(callback: CallbackQuery):
    user_id = int(callback.data.split("_")[2])
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT banned FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        new = 0 if row[0] else 1
        await db.execute("UPDATE users SET banned=? WHERE user_id=?", (new, user_id))
        await db.commit()
    await callback.answer(f"User {'banned' if new else 'unbanned'}")

@dp.callback_query_handler(lambda c: c.data == "admin_list_users")
async def list_users(callback: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT user_id, full_name, balance FROM users LIMIT 20")
        users = await cur.fetchall()
    
    text = "👥 **Users (first 20):**\n"
    for u in users:
        text += f"\n`{u['user_id']}` | {u['full_name']} | `{u['balance']}`"
    
    await callback.message.edit_text(text, parse_mode=ParseMode.MARKDOWN)

@dp.callback_query_handler(lambda c: c.data == "admin_settings")
async def settings_menu(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(row_width=2)
    settings_list = [
        ("Welcome Msg", "set_welcome"),
        ("Currency", "set_currency"),
        ("Support", "set_support"),
        ("Rules", "set_rules"),
        ("Force Join", "set_force_join"),
        ("Captcha", "set_captcha"),
        ("Shop Enable", "set_shop_enabled"),
        ("Referral", "set_ref_reward"),
        ("Daily Amount", "set_daily_bonus_amount"),
        ("Daily Enable", "set_daily_bonus_enabled"),
        ("Scratch Rewards", "set_scratch_rewards"),
        ("Scratch Enable", "set_scratch_enabled"),
    ]
    for name, cb in settings_list:
        kb.insert(InlineKeyboardButton(name, callback_data=cb))
    
    await callback.message.edit_text("⚙️ **Settings**", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data.startswith("set_"))
async def set_value(callback: CallbackQuery, state: FSMContext):
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
        return await callback.answer("Error")
    
    await state.update_data(setting_key=key)
    await callback.message.reply(f"✏️ **New value for {key}:**")
    await AdminStates.waiting_for_setting_value.set()
    await callback.answer()

@dp.message_handler(state=AdminStates.waiting_for_setting_value)
async def save_setting(message: Message, state: FSMContext):
    value = message.text
    data = await state.get_data()
    key = data['setting_key']
    await update_setting(key, value)
    await message.reply(f"✅ {key} updated")
    await state.finish()

@dp.callback_query_handler(lambda c: c.data == "admin_promos")
async def promos_menu(callback: CallbackQuery):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("➕ Create", callback_data="admin_create_promo"))
    kb.add(InlineKeyboardButton("📋 List", callback_data="admin_list_promos"))
    await callback.message.edit_text("🎁 **Promos**", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data == "admin_create_promo")
async def create_promo(callback: CallbackQuery, state: FSMContext):
    await callback.message.reply("📝 **Code:**")
    await AdminStates.waiting_for_promo_code.set()
    await callback.answer()

@dp.message_handler(state=AdminStates.waiting_for_promo_code)
async def promo_code(message: Message, state: FSMContext):
    await state.update_data(code=message.text)
    await message.reply("💰 **Reward:**")
    await AdminStates.waiting_for_promo_reward.set()

@dp.message_handler(state=AdminStates.waiting_for_promo_reward)
async def promo_reward(message: Message, state: FSMContext):
    try:
        reward = float(message.text)
        await state.update_data(reward=reward)
        await message.reply("🔢 **Max usage** (-1 unlimited):")
        await AdminStates.waiting_for_promo_limit.set()
    except:
        await message.reply("❌ Invalid number")

@dp.message_handler(state=AdminStates.waiting_for_promo_limit)
async def promo_limit(message: Message, state: FSMContext):
    try:
        limit = int(message.text)
        data = await state.get_data()
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("INSERT OR REPLACE INTO promos (code, reward, max_usage, expiry_date) VALUES (?,?,?,?)",
                            (data['code'], data['reward'], limit, "2099-12-31"))
            await db.commit()
        await message.reply(f"✅ Promo {data['code']} created")
        await state.finish()
    except:
        await message.reply("❌ Invalid number")

@dp.callback_query_handler(lambda c: c.data == "admin_list_promos")
async def list_promos(callback: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM promos")
        promos = await cur.fetchall()
    
    if not promos:
        return await callback.message.edit_text("📭 No promos")
    
    for p in promos:
        text = f"Code: `{p['code']}`\nReward: {p['reward']}\nUsed: {p['used_count']}/{p['max_usage']}\nExpiry: {p['expiry_date']}"
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("🗑️ Delete", callback_data=f"admin_del_promo_{p['code']}"))
        await callback.message.answer(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    
    await callback.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("admin_del_promo_"))
async def del_promo(callback: CallbackQuery):
    code = callback.data.split("_")[3]
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM promos WHERE code=?", (code,))
        await db.commit()
    await callback.answer("🗑️ Deleted")
    await callback.message.delete()

@dp.callback_query_handler(lambda c: c.data == "admin_tasks")
async def tasks_admin_menu(callback: CallbackQuery):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("➕ Create", callback_data="admin_create_task"))
    kb.add(InlineKeyboardButton("📋 List", callback_data="admin_list_tasks"))
    await callback.message.edit_text("📋 **Tasks**", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data == "admin_create_task")
async def create_task(callback: CallbackQuery, state: FSMContext):
    await callback.message.reply("📝 **Description:**")
    await AdminStates.waiting_for_task_desc.set()
    await callback.answer()

@dp.message_handler(state=AdminStates.waiting_for_task_desc)
async def task_desc(message: Message, state: FSMContext):
    await state.update_data(desc=message.text)
    await message.reply("🔗 **Link** (or None):")
    await AdminStates.waiting_for_task_link.set()

@dp.message_handler(state=AdminStates.waiting_for_task_link)
async def task_link(message: Message, state: FSMContext):
    await state.update_data(link=message.text)
    await message.reply("💰 **Reward:**")
    await AdminStates.waiting_for_task_reward.set()

@dp.message_handler(state=AdminStates.waiting_for_task_reward)
async def task_reward(message: Message, state: FSMContext):
    try:
        reward = float(message.text)
        data = await state.get_data()
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("INSERT INTO tasks (description, link, reward) VALUES (?,?,?)",
                            (data['desc'], data['link'], reward))
            await db.commit()
        await message.reply("✅ Task created")
        await state.finish()
    except:
        await message.reply("❌ Invalid number")

@dp.callback_query_handler(lambda c: c.data == "admin_list_tasks")
async def list_tasks(callback: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM tasks")
        tasks = await cur.fetchall()
    
    if not tasks:
        return await callback.message.edit_text("📭 No tasks")
    
    for t in tasks:
        text = f"ID: {t['id']}\nDesc: {t['description']}\nLink: {t['link']}\nReward: {t['reward']}"
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("🗑️ Delete", callback_data=f"admin_del_task_{t['id']}"))
        await callback.message.answer(text, reply_markup=kb)
    
    await callback.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("admin_del_task_"))
async def del_task(callback: CallbackQuery):
    task_id = int(callback.data.split("_")[3])
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        await db.commit()
    await callback.answer("🗑️ Deleted")
    await callback.message.delete()

@dp.callback_query_handler(lambda c: c.data == "admin_orders")
async def admin_orders(callback: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT orders.*, products.name FROM orders 
            JOIN products ON orders.product_id=products.id 
            WHERE status='pending'
        """)
        orders = await cur.fetchall()
    
    if not orders:
        return await callback.message.edit_text("📭 No pending orders")
    
    for o in orders:
        text = f"📦 Order #{o['id']}\nProduct: {o['name']}\nUser: {o['user_id']}\nData: {o['data']}"
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("✅ Deliver", callback_data=f"admin_deliver_order_{o['id']}"))
        await callback.message.answer(text, reply_markup=kb)
    
    await callback.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("admin_deliver_order_"))
async def deliver_order(callback: CallbackQuery):
    order_id = int(callback.data.split("_")[4])
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE orders SET status='delivered' WHERE id=?", (order_id,))
        await db.commit()
    await callback.message.edit_text(f"✅ Order #{order_id} delivered")
    await callback.answer()

@dp.callback_query_handler(lambda c: c.data == "admin_panel")
async def back_to_admin(callback: CallbackQuery):
    await callback.message.edit_text("🔧 **Admin Panel**", reply_markup=admin_panel_kb())

@dp.callback_query_handler(lambda c: c.data == "shop_main")
async def back_to_shop(callback: CallbackQuery):
    await shop(callback.message)

# ---------- Main ----------
async def main():
    await init_db()
    
    # Start Flask in thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    print(f"🤖 Bot running on port {PORT}")
    await dp.start_polling()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("👋 Stopped")
