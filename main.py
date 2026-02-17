import os
import json
import sqlite3
import random
import threading
from datetime import datetime, date
from functools import wraps

from flask import Flask, jsonify
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, ParseMode
)
from telegram.ext import (
    Updater, CommandHandler, MessageHandler, Filters,
    CallbackContext, CallbackQueryHandler, ConversationHandler
)

# ---------- Configuration ----------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
PORT = int(os.getenv("PORT", 10000))
DB_NAME = "shop_bot.db"

# ---------- Flask Health Check ----------
app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({
        "status": "running",
        "bot": "Telegram Shop Bot",
        "version": "8.0",
        "python": "3.9",
        "timestamp": datetime.now().isoformat()
    })

@app.route('/health')
def health():
    return jsonify({"status": "healthy"}), 200

def run_flask():
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)

# ---------- Database Setup ----------
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    # Settings
    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    
    defaults = {
        "welcome_message": "🌟 Welcome to Premium Shop, {name}!",
        "currency": "💎 Credits",
        "support_link": "https://t.me/telegram",
        "rules": "📜 Rules:\n1. No spamming\n2. Be respectful",
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
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, val))

    # Users
    c.execute("""
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
    c.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE
        )
    """)
    
    # Products
    c.execute("""
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
    c.execute("""
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
    c.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            description TEXT,
            link TEXT,
            reward REAL
        )
    """)
    
    # Completed Tasks
    c.execute("""
        CREATE TABLE IF NOT EXISTS completed_tasks (
            user_id INTEGER,
            task_id INTEGER,
            PRIMARY KEY (user_id, task_id)
        )
    """)
    
    # Promos
    c.execute("""
        CREATE TABLE IF NOT EXISTS promos (
            code TEXT PRIMARY KEY,
            reward REAL,
            max_usage INTEGER,
            used_count INTEGER DEFAULT 0,
            expiry_date TEXT
        )
    """)
    
    # Promo Usage
    c.execute("""
        CREATE TABLE IF NOT EXISTS promo_usage (
            user_id INTEGER,
            code TEXT,
            PRIMARY KEY (user_id, code)
        )
    """)

    # Transactions
    c.execute("""
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
    c.execute("""
        CREATE TABLE IF NOT EXISTS daily_bonus (
            user_id INTEGER PRIMARY KEY,
            last_claim DATE
        )
    """)

    # Daily Scratch tracking
    c.execute("""
        CREATE TABLE IF NOT EXISTS daily_scratch (
            user_id INTEGER PRIMARY KEY,
            last_scratch DATE
        )
    """)

    conn.commit()
    conn.close()

# ---------- Database Helpers ----------
def get_setting(key):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def update_setting(key, value):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()

def get_user(user_id):
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def add_user(user_id, username, full_name, referrer_id=None):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    if c.fetchone():
        conn.close()
        return

    c.execute("""
        INSERT INTO users (user_id, username, full_name, referrer_id)
        VALUES (?, ?, ?, ?)
    """, (user_id, username, full_name, referrer_id))
    
    if referrer_id:
        reward = float(get_setting("referral_reward") or 0)
        c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (reward, referrer_id))
        c.execute("""
            INSERT INTO transactions (user_id, amount, type, description)
            VALUES (?, ?, 'referral', ?)
        """, (referrer_id, reward, f"Referral from {full_name}"))
    
    conn.commit()
    conn.close()

# ---------- Conversation States ----------
(CAPTCHA, ADDRESS, PROMO_CODE, 
 CATEGORY_NAME, CATEGORY_EDIT, 
 PRODUCT_NAME, PRODUCT_DESC, PRODUCT_PRICE, PRODUCT_TYPE, PRODUCT_CONTENT, PRODUCT_STOCK,
 USER_SEARCH, BALANCE_CHANGE,
 SETTING_VALUE,
 TASK_DESC, TASK_LINK, TASK_REWARD,
 PROMO_REWARD, PROMO_LIMIT) = range(18)

# ---------- Emoji Captcha ----------
EMOJI_LIST = ["😀", "😂", "😍", "🥺", "😎", "🎉", "🔥", "⭐", "🐶", "🐱", "🐼"]

def generate_emoji_captcha():
    seq = random.sample(EMOJI_LIST, 4)
    missing = random.randint(0, 3)
    answer = seq[missing]
    seq[missing] = "___"
    return " ".join(seq), answer

# ---------- Admin Decorator ----------
def admin_only(func):
    @wraps(func)
    def wrapper(update: Update, context: CallbackContext, *args, **kwargs):
        if update.effective_user.id != ADMIN_ID:
            update.message.reply_text("⛔ Access denied")
            return
        return func(update, context, *args, **kwargs)
    return wrapper

# ---------- Keyboards ----------
def main_menu_kb(is_admin=False):
    kb = [
        ["🛍 Shop", "👤 Profile"],
        ["🎁 Daily Bonus", "🎲 Scratch Card"],
        ["📋 Tasks", "ℹ️ Support"],
        ["📜 Rules"]
    ]
    if is_admin:
        kb.append(["⚙️ Admin Panel"])
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)

def admin_panel_kb():
    kb = [
        [InlineKeyboardButton("📊 Stats", callback_data="admin_stats"),
         InlineKeyboardButton("👥 Users", callback_data="admin_users")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"),
         InlineKeyboardButton("🛍 Shop", callback_data="admin_shop")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="admin_settings"),
         InlineKeyboardButton("🎁 Promos", callback_data="admin_promos")],
        [InlineKeyboardButton("📋 Tasks", callback_data="admin_tasks"),
         InlineKeyboardButton("📦 Orders", callback_data="admin_orders")],
        [InlineKeyboardButton("📦 Backup DB", callback_data="admin_backup")]
    ]
    return InlineKeyboardMarkup(kb)

# ---------- Start Command ----------
def start(update: Update, context: CallbackContext):
    user = update.effective_user
    args = context.args
    referrer_id = None
    if args and args[0].isdigit():
        ref = int(args[0])
        if ref != user.id and get_user(ref):
            referrer_id = ref

    add_user(user.id, user.username, user.full_name, referrer_id)

    if get_setting("captcha_enabled") == "1":
        q, a = generate_emoji_captcha()
        context.user_data['captcha_answer'] = a
        update.message.reply_text(
            f"🔒 **Captcha:**\n`{q}`\n\nType missing emoji:",
            parse_mode=ParseMode.MARKDOWN
        )
        return CAPTCHA
    
    show_welcome(update, context)
    return ConversationHandler.END

def captcha_handler(update: Update, context: CallbackContext):
    if update.message.text.strip() == context.user_data.get('captcha_answer'):
        show_welcome(update, context)
        return ConversationHandler.END
    else:
        update.message.reply_text("❌ Wrong. Try again.")
        q, a = generate_emoji_captcha()
        context.user_data['captcha_answer'] = a
        update.message.reply_text(f"New:\n`{q}`\n\nType missing:", parse_mode=ParseMode.MARKDOWN)
        return CAPTCHA

def show_welcome(update: Update, context: CallbackContext):
    welcome = get_setting("welcome_message")
    user = get_user(update.effective_user.id)
    if user and user['banned']:
        update.message.reply_text("🚫 You are banned.")
        return
    welcome = welcome.replace("{name}", update.effective_user.full_name)
    is_admin = update.effective_user.id == ADMIN_ID
    update.message.reply_text(
        welcome,
        reply_markup=main_menu_kb(is_admin),
        parse_mode=ParseMode.MARKDOWN
    )

# ---------- Profile ----------
def profile(update: Update, context: CallbackContext):
    user = get_user(update.effective_user.id)
    if not user:
        update.message.reply_text("User not found. Use /start")
        return
    
    spent = user['total_spent']
    if spent < 100: level = "🥉 Bronze"
    elif spent < 500: level = "🥈 Silver"
    else: level = "🥇 Gold"
    
    text = f"""👤 **Profile**
ID: `{user['user_id']}`
💰 Balance: `{user['balance']}` {get_setting('currency')}
📊 Spent: `{spent}` Credits
🏅 Level: {level}
📅 Joined: {user['joined_at']}"""
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Orders", callback_data="my_orders"),
         InlineKeyboardButton("📜 History", callback_data="my_transactions")],
        [InlineKeyboardButton("🎁 Redeem", callback_data="redeem_promo")]
    ])
    
    update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    
    bot_info = context.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={user['user_id']}"
    update.message.reply_text(f"🔗 **Referral Link:**\n`{ref_link}`", parse_mode=ParseMode.MARKDOWN)

def my_transactions_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id
    
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM transactions WHERE user_id=? ORDER BY created_at DESC LIMIT 10", (user_id,))
    txs = c.fetchall()
    conn.close()
    
    if not txs:
        query.edit_message_text("📭 No transactions")
        return
    
    text = "📊 **Transactions:**\n"
    for t in txs:
        sign = "+" if t['amount'] > 0 else ""
        text += f"\n• {t['created_at'][:10]} {t['type']}: {sign}{t['amount']} Credits"
    
    query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)

# ---------- Daily Bonus ----------
def daily_bonus(update: Update, context: CallbackContext):
    if get_setting("daily_bonus_enabled") != "1":
        update.message.reply_text("❌ Daily bonus disabled")
        return
    
    user_id = update.effective_user.id
    today = date.today().isoformat()
    
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT last_claim FROM daily_bonus WHERE user_id=?", (user_id,))
    row = c.fetchone()
    if row and row[0] == today:
        conn.close()
        update.message.reply_text("⏳ Already claimed today")
        return
    
    amount = float(get_setting("daily_bonus_amount"))
    c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, user_id))
    c.execute("INSERT OR REPLACE INTO daily_bonus VALUES (?,?)", (user_id, today))
    c.execute("INSERT INTO transactions (user_id,amount,type,description) VALUES (?,?,'daily','Daily bonus')", (user_id, amount))
    conn.commit()
    conn.close()
    
    update.message.reply_text(f"🎉 **+{amount} Credits**", parse_mode=ParseMode.MARKDOWN)

# ---------- Scratch Card ----------
def scratch_card(update: Update, context: CallbackContext):
    if get_setting("scratch_enabled") != "1":
        update.message.reply_text("❌ Scratch disabled")
        return
    
    user_id = update.effective_user.id
    today = date.today().isoformat()
    
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT last_scratch FROM daily_scratch WHERE user_id=?", (user_id,))
    row = c.fetchone()
    if row and row[0] == today:
        conn.close()
        update.message.reply_text("⏳ Already scratched today")
        return
    
    rewards = [float(x.strip()) for x in get_setting("scratch_rewards").split(",") if x.strip()]
    amount = random.choice(rewards) if rewards else 10
    
    c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, user_id))
    c.execute("INSERT OR REPLACE INTO daily_scratch VALUES (?,?)", (user_id, today))
    c.execute("INSERT INTO transactions (user_id,amount,type,description) VALUES (?,?,'scratch','Scratch card')", (user_id, amount))
    conn.commit()
    conn.close()
    
    update.message.reply_text(f"🎲 **You won {amount} Credits!**", parse_mode=ParseMode.MARKDOWN)

# ---------- Tasks ----------
def tasks_list(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM tasks WHERE id NOT IN (SELECT task_id FROM completed_tasks WHERE user_id=?)", (user_id,))
    tasks = c.fetchall()
    conn.close()
    
    if not tasks:
        update.message.reply_text("✅ No tasks available")
        return
    
    text = "📋 **Tasks:**\n"
    kb = []
    for t in tasks:
        text += f"\n🔹 {t['description']} – {t['reward']} Credits"
        kb.append([InlineKeyboardButton(f"✅ Complete #{t['id']}", callback_data=f"do_task_{t['id']}")])
    
    update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

def do_task_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    task_id = int(query.data.split("_")[2])
    user_id = query.from_user.id
    
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM tasks WHERE id=?", (task_id,))
    task = c.fetchone()
    if not task:
        conn.close()
        query.edit_message_text("Task not found")
        return
    
    c.execute("SELECT * FROM completed_tasks WHERE user_id=? AND task_id=?", (user_id, task_id))
    if c.fetchone():
        conn.close()
        query.edit_message_text("Already done")
        return
    
    c.execute("INSERT INTO completed_tasks VALUES (?,?)", (user_id, task_id))
    c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (task['reward'], user_id))
    c.execute("INSERT INTO transactions (user_id,amount,type,description) VALUES (?,?,'task',?)", 
              (user_id, task['reward'], task['description']))
    conn.commit()
    conn.close()
    
    query.edit_message_text(f"✅ **Task done!** +{task['reward']} Credits", parse_mode=ParseMode.MARKDOWN)

# ---------- Promo ----------
def promo_ask(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    query.message.reply_text("🎁 **Enter promo code:**")
    return PROMO_CODE

def promo_redeem(update: Update, context: CallbackContext):
    code = update.message.text.strip()
    user_id = update.effective_user.id
    
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM promos WHERE code=?", (code,))
    promo = c.fetchone()
    
    if not promo:
        conn.close()
        update.message.reply_text("❌ Invalid code")
        return ConversationHandler.END
    
    if promo['expiry_date'] and promo['expiry_date'] < datetime.now().strftime("%Y-%m-%d"):
        conn.close()
        update.message.reply_text("❌ Expired")
        return ConversationHandler.END
    
    if promo['max_usage'] != -1 and promo['used_count'] >= promo['max_usage']:
        conn.close()
        update.message.reply_text("❌ Limit reached")
        return ConversationHandler.END
    
    c.execute("SELECT * FROM promo_usage WHERE user_id=? AND code=?", (user_id, code))
    if c.fetchone():
        conn.close()
        update.message.reply_text("❌ Already used")
        return ConversationHandler.END
    
    c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (promo['reward'], user_id))
    c.execute("UPDATE promos SET used_count=used_count+1 WHERE code=?", (code,))
    c.execute("INSERT INTO promo_usage VALUES (?,?)", (user_id, code))
    c.execute("INSERT INTO transactions (user_id,amount,type,description) VALUES (?,?,'promo',?)", 
              (user_id, promo['reward'], f"Promo {code}"))
    conn.commit()
    conn.close()
    
    update.message.reply_text(f"✅ **+{promo['reward']} Credits**", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

# ---------- Shop ----------
def shop(update: Update, context: CallbackContext):
    if get_setting("shop_enabled") != "1":
        update.message.reply_text("⚠️ Shop disabled")
        return
    
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM categories")
    cats = c.fetchall()
    conn.close()
    
    if not cats:
        update.message.reply_text("📭 No categories")
        return
    
    kb = [[InlineKeyboardButton(c['name'], callback_data=f"cat_{c['id']}")] for c in cats]
    update.message.reply_text("📂 **Categories:**", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

def category_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    cat_id = int(query.data.split("_")[1])
    
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM products WHERE category_id=?", (cat_id,))
    prods = c.fetchall()
    conn.close()
    
    if not prods:
        query.edit_message_text("📭 No products")
        return
    
    kb = []
    for p in prods:
        stock = "∞" if p['stock'] == -1 else p['stock']
        kb.append([InlineKeyboardButton(f"{p['name']} | {p['price']} | Stock:{stock}", callback_data=f"prod_{p['id']}")])
    
    query.edit_message_text("📦 **Products:**", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

def product_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    prod_id = int(query.data.split("_")[1])
    
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM products WHERE id=?", (prod_id,))
    prod = c.fetchone()
    conn.close()
    
    if not prod:
        query.edit_message_text("Product not found")
        return
    
    text = f"""📦 **{prod['name']}**
{prod['description']}
💰 Price: `{prod['price']}` {get_setting('currency')}
📦 Stock: `{'Unlimited' if prod['stock']==-1 else prod['stock']}`
Type: `{prod['type']}`"""
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Buy", callback_data=f"buy_{prod['id']}")],
        [InlineKeyboardButton("🔙 Back", callback_data=f"back_to_cat_{prod['category_id']}")]
    ])
    
    query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    context.user_data['current_product'] = dict(prod)

def buy_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    prod_id = int(query.data.split("_")[1])
    user_id = query.from_user.id
    
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM products WHERE id=?", (prod_id,))
    prod = c.fetchone()
    c.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    user = c.fetchone()
    conn.close()
    
    if not prod or not user:
        query.edit_message_text("Error")
        return
    if prod['stock'] != -1 and prod['stock'] <= 0:
        query.edit_message_text("Out of stock")
        return
    if user['balance'] < prod['price']:
        query.edit_message_text("Insufficient balance")
        return
    
    if prod['type'] == 'physical':
        context.user_data['buy_prod_id'] = prod_id
        query.message.reply_text("📦 **Enter shipping address:**")
        return ADDRESS
    
    # Process digital/file purchase
    new_balance = user['balance'] - prod['price']
    new_stock = prod['stock'] - 1 if prod['stock'] != -1 else -1
    
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE users SET balance=?, total_spent=total_spent+? WHERE user_id=?", 
              (new_balance, prod['price'], user_id))
    if new_stock != -1:
        c.execute("UPDATE products SET stock=? WHERE id=?", (new_stock, prod_id))
    
    status = "delivered" if prod['type'] in ('digital','file') else "pending"
    data = prod['content'] if status == "delivered" else ""
    c.execute("INSERT INTO orders (user_id,product_id,status,data) VALUES (?,?,?,?)",
              (user_id, prod_id, status, data))
    c.execute("INSERT INTO transactions (user_id,amount,type,description) VALUES (?,?,'purchase',?)",
              (user_id, -prod['price'], f"Bought {prod['name']}"))
    conn.commit()
    conn.close()
    
    context.bot.send_message(user_id, f"✅ **Purchase done!** Balance: {new_balance} Credits", parse_mode=ParseMode.MARKDOWN)
    
    if prod['type'] == 'file':
        try:
            context.bot.send_document(user_id, prod['content'], caption=f"📁 {prod['name']}")
        except:
            context.bot.send_message(user_id, "❌ File delivery failed")
    elif prod['type'] == 'digital':
        context.bot.send_message(user_id, f"📦 **Your item:**\n`{prod['content']}`", parse_mode=ParseMode.MARKDOWN)
    
    return ConversationHandler.END

def address_handler(update: Update, context: CallbackContext):
    addr = update.message.text
    prod_id = context.user_data['buy_prod_id']
    
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM products WHERE id=?", (prod_id,))
    prod = c.fetchone()
    c.execute("SELECT * FROM users WHERE user_id=?", (update.effective_user.id,))
    user = c.fetchone()
    conn.close()
    
    new_balance = user['balance'] - prod['price']
    new_stock = prod['stock'] - 1 if prod['stock'] != -1 else -1
    
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE users SET balance=?, total_spent=total_spent+? WHERE user_id=?", 
              (new_balance, prod['price'], update.effective_user.id))
    if new_stock != -1:
        c.execute("UPDATE products SET stock=? WHERE id=?", (new_stock, prod_id))
    
    c.execute("INSERT INTO orders (user_id,product_id,status,data) VALUES (?,?,?,?)",
              (update.effective_user.id, prod_id, 'pending', addr))
    c.execute("INSERT INTO transactions (user_id,amount,type,description) VALUES (?,?,'purchase',?)",
              (update.effective_user.id, -prod['price'], f"Bought {prod['name']}"))
    conn.commit()
    conn.close()
    
    update.message.reply_text(f"✅ **Order placed!** Balance: {new_balance} Credits\nAdmin will review your order.", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

def my_orders_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id
    
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT orders.*, products.name FROM orders JOIN products ON orders.product_id=products.id WHERE user_id=? ORDER BY created_at DESC LIMIT 5", (user_id,))
    orders = c.fetchall()
    conn.close()
    
    if not orders:
        query.edit_message_text("📭 No orders")
        return
    
    text = "📦 **Recent Orders:**\n"
    for o in orders:
        text += f"\n🔹 {o['name']} – {o['status'].upper()}"
    
    query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)

# ---------- Support & Rules ----------
def support(update: Update, context: CallbackContext):
    link = get_setting("support_link")
    update.message.reply_text(f"ℹ️ **Contact Support:** {link}", parse_mode=ParseMode.MARKDOWN)

def rules(update: Update, context: CallbackContext):
    rules_text = get_setting("rules")
    update.message.reply_text(rules_text, parse_mode=ParseMode.MARKDOWN)

# ---------- Admin Panel ----------
@admin_only
def admin_panel(update: Update, context: CallbackContext):
    update.message.reply_text("🔧 **Admin Panel**", reply_markup=admin_panel_kb(), parse_mode=ParseMode.MARKDOWN)

def admin_stats_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM orders")
    orders = c.fetchone()[0]
    c.execute("SELECT SUM(price) FROM products JOIN orders ON products.id=orders.product_id")
    revenue = c.fetchone()[0] or 0
    conn.close()
    
    text = f"""📊 **Stats**
👥 Users: `{users}`
📦 Orders: `{orders}`
💰 Revenue: `{revenue}` {get_setting('currency')}"""
    
    query.edit_message_text(text, reply_markup=admin_panel_kb(), parse_mode=ParseMode.MARKDOWN)

def admin_backup_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    query.edit_message_text("📦 Backup feature coming soon...")

def admin_broadcast_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    query.message.reply_text("📝 **Send broadcast message:**")

def admin_shop_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Category", callback_data="admin_add_cat")],
        [InlineKeyboardButton("📋 List Categories", callback_data="admin_list_cats")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]
    ])
    query.edit_message_text("🛍 **Shop Management**", reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

def admin_add_cat_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    query.message.reply_text("📝 **Category name:**")
    return CATEGORY_NAME

def add_category(update: Update, context: CallbackContext):
    name = update.message.text
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (name,))
    conn.commit()
    conn.close()
    update.message.reply_text(f"✅ Category '{name}' added")
    return ConversationHandler.END

def admin_list_cats_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM categories")
    cats = c.fetchall()
    conn.close()
    
    if not cats:
        query.edit_message_text("📭 No categories")
        return
    
    kb = []
    for c in cats:
        kb.append([
            InlineKeyboardButton(f"📁 {c['name']}", callback_data=f"admin_cat_{c['id']}"),
            InlineKeyboardButton("✏️", callback_data=f"admin_edit_cat_{c['id']}"),
            InlineKeyboardButton("🗑️", callback_data=f"admin_del_cat_{c['id']}")
        ])
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="admin_shop")])
    
    query.edit_message_text("📂 **Categories:**", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

def admin_cat_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    cat_id = int(query.data.split("_")[2])
    
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM products WHERE category_id=?", (cat_id,))
    prods = c.fetchall()
    conn.close()
    
    kb = []
    for p in prods:
        kb.append([
            InlineKeyboardButton(f"{p['name']} | {p['price']}", callback_data=f"admin_prod_{p['id']}"),
            InlineKeyboardButton("✏️", callback_data=f"admin_edit_prod_{p['id']}"),
            InlineKeyboardButton("🗑️", callback_data=f"admin_del_prod_{p['id']}")
        ])
    kb.append([InlineKeyboardButton("➕ Add Product", callback_data=f"admin_add_prod_cat_{cat_id}")])
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="admin_list_cats")])
    
    query.edit_message_text("📦 **Products:**", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

def admin_edit_cat_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    cat_id = int(query.data.split("_")[3])
    context.user_data['edit_cat_id'] = cat_id
    query.message.reply_text("✏️ **New name:**")
    return CATEGORY_EDIT

def edit_category(update: Update, context: CallbackContext):
    new_name = update.message.text
    cat_id = context.user_data['edit_cat_id']
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE categories SET name=? WHERE id=?", (new_name, cat_id))
    conn.commit()
    conn.close()
    update.message.reply_text("✅ Category updated")
    return ConversationHandler.END

def admin_del_cat_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    cat_id = int(query.data.split("_")[3])
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM categories WHERE id=?", (cat_id,))
    conn.commit()
    conn.close()
    query.edit_message_text("🗑️ Category deleted")

def admin_add_prod_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    cat_id = int(query.data.split("_")[4])
    context.user_data['prod_cat_id'] = cat_id
    query.message.reply_text("📝 **Product name:**")
    return PRODUCT_NAME

def add_product_name(update: Update, context: CallbackContext):
    context.user_data['prod_name'] = update.message.text
    update.message.reply_text("📝 **Description:**")
    return PRODUCT_DESC

def add_product_desc(update: Update, context: CallbackContext):
    context.user_data['prod_desc'] = update.message.text
    update.message.reply_text("💰 **Price:**")
    return PRODUCT_PRICE

def add_product_price(update: Update, context: CallbackContext):
    try:
        price = float(update.message.text)
        context.user_data['prod_price'] = price
        kb = ReplyKeyboardMarkup([["digital", "file", "physical"]], one_time_keyboard=True, resize_keyboard=True)
        update.message.reply_text("📦 **Type:**", reply_markup=kb)
        return PRODUCT_TYPE
    except:
        update.message.reply_text("❌ Invalid price")
        return PRODUCT_PRICE

def add_product_type(update: Update, context: CallbackContext):
    ptype = update.message.text.lower()
    if ptype not in ('digital','file','physical'):
        update.message.reply_text("❌ Invalid type")
        return PRODUCT_TYPE
    
    context.user_data['prod_type'] = ptype
    
    if ptype == 'physical':
        context.user_data['prod_content'] = "Physical item"
        update.message.reply_text("📦 **Stock** (-1 unlimited):", reply_markup=ReplyKeyboardMarkup.remove_keyboard())
        return PRODUCT_STOCK
    else:
        update.message.reply_text("📄 **Content** (text or file):", reply_markup=ReplyKeyboardMarkup.remove_keyboard())
        return PRODUCT_CONTENT

def add_product_content(update: Update, context: CallbackContext):
    if update.message.document:
        content = update.message.document.file_id
    elif update.message.photo:
        content = update.message.photo[-1].file_id
    else:
        content = update.message.text or "No content"
    
    context.user_data['prod_content'] = content
    update.message.reply_text("📦 **Stock** (-1 unlimited):")
    return PRODUCT_STOCK

def add_product_stock(update: Update, context: CallbackContext):
    try:
        stock = int(update.message.text)
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("""
            INSERT INTO products (category_id, name, description, price, type, content, stock)
            VALUES (?,?,?,?,?,?,?)
        """, (context.user_data['prod_cat_id'], context.user_data['prod_name'],
              context.user_data['prod_desc'], context.user_data['prod_price'],
              context.user_data['prod_type'], context.user_data['prod_content'], stock))
        conn.commit()
        conn.close()
        update.message.reply_text("✅ **Product added!**", parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END
    except:
        update.message.reply_text("❌ Invalid stock")
        return PRODUCT_STOCK

def admin_edit_prod_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    prod_id = int(query.data.split("_")[3])
    context.user_data['edit_prod_id'] = prod_id
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Name", callback_data="edit_name"),
         InlineKeyboardButton("Description", callback_data="edit_desc")],
        [InlineKeyboardButton("Price", callback_data="edit_price"),
         InlineKeyboardButton("Stock", callback_data="edit_stock")],
        [InlineKeyboardButton("Content", callback_data="edit_content"),
         InlineKeyboardButton("Type", callback_data="edit_type")]
    ])
    
    query.message.reply_text("✏️ **Edit field:**", reply_markup=kb)
    return PRODUCT_TYPE

def admin_edit_field_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    field = query.data.replace("edit_", "")
    context.user_data['edit_field'] = field
    query.message.reply_text(f"✏️ **New value for {field}:**")
    return PRODUCT_NAME

def admin_update_product(update: Update, context: CallbackContext):
    value = update.message.text
    prod_id = context.user_data['edit_prod_id']
    field = context.user_data['edit_field']
    
    if field in ('price', 'stock'):
        try:
            value = float(value) if field=='price' else int(value)
        except:
            update.message.reply_text("❌ Invalid number")
            return PRODUCT_NAME
    
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(f"UPDATE products SET {field}=? WHERE id=?", (value, prod_id))
    conn.commit()
    conn.close()
    
    update.message.reply_text("✅ Product updated")
    return ConversationHandler.END

def admin_del_prod_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    prod_id = int(query.data.split("_")[3])
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM products WHERE id=?", (prod_id,))
    conn.commit()
    conn.close()
    query.edit_message_text("🗑️ Product deleted")

def admin_users_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Search", callback_data="admin_search_user")],
        [InlineKeyboardButton("📋 List", callback_data="admin_list_users")]
    ])
    query.edit_message_text("👥 **Users**", reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

def admin_search_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    query.message.reply_text("🆔 **User ID or username:**")
    return USER_SEARCH

def admin_show_user(update: Update, context: CallbackContext):
    query = update.message.text
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if query.isdigit():
        c.execute("SELECT * FROM users WHERE user_id=?", (int(query),))
    else:
        c.execute("SELECT * FROM users WHERE username=?", (query.replace('@',''),))
    user = c.fetchone()
    conn.close()
    
    if not user:
        update.message.reply_text("❌ Not found")
        return ConversationHandler.END
    
    text = f"""👤 **User**
ID: `{user['user_id']}`
Name: {user['full_name']}
Username: @{user['username']}
Balance: `{user['balance']}`
Banned: `{bool(user['banned'])}`"""
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Give", callback_data=f"admin_give_bal_{user['user_id']}"),
         InlineKeyboardButton("🚫 Ban", callback_data=f"admin_ban_{user['user_id']}")]
    ])
    
    update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

def admin_give_bal_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    user_id = int(query.data.split("_")[3])
    context.user_data['target_user_id'] = user_id
    query.message.reply_text("💰 **Amount (+/-):**")
    return BALANCE_CHANGE

def admin_update_balance(update: Update, context: CallbackContext):
    try:
        amount = float(update.message.text)
        user_id = context.user_data['target_user_id']
        
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, user_id))
        conn.commit()
        conn.close()
        
        update.message.reply_text(f"✅ Balance updated by {amount}")
        return ConversationHandler.END
    except:
        update.message.reply_text("❌ Invalid amount")
        return BALANCE_CHANGE

def admin_ban_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    user_id = int(query.data.split("_")[2])
    
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT banned FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    new = 0 if row[0] else 1
    c.execute("UPDATE users SET banned=? WHERE user_id=?", (new, user_id))
    conn.commit()
    conn.close()
    
    query.edit_message_text(f"User {'banned' if new else 'unbanned'}")

def admin_list_users_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT user_id, full_name, balance FROM users LIMIT 20")
    users = c.fetchall()
    conn.close()
    
    text = "👥 **Users (first 20):**\n"
    for u in users:
        text += f"\n`{u['user_id']}` | {u['full_name']} | `{u['balance']}`"
    
    query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)

def admin_settings_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    
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
    
    kb = []
    for name, cb in settings_list:
        kb.append([InlineKeyboardButton(name, callback_data=cb)])
    
    query.edit_message_text("⚙️ **Settings**", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

def admin_setting_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    
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
    
    key = key_map.get(query.data)
    if not key:
        return
    
    context.user_data['setting_key'] = key
    query.message.reply_text(f"✏️ **New value for {key}:**")
    return SETTING_VALUE

def admin_save_setting(update: Update, context: CallbackContext):
    value = update.message.text
    key = context.user_data['setting_key']
    update_setting(key, value)
    update.message.reply_text(f"✅ {key} updated")
    return ConversationHandler.END

def admin_promos_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Create", callback_data="admin_create_promo")],
        [InlineKeyboardButton("📋 List", callback_data="admin_list_promos")]
    ])
    query.edit_message_text("🎁 **Promos**", reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

def admin_create_promo_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    query.message.reply_text("📝 **Code:**")
    return PROMO_CODE

def add_promo_code(update: Update, context: CallbackContext):
    context.user_data['promo_code'] = update.message.text
    update.message.reply_text("💰 **Reward:**")
    return PROMO_REWARD

def add_promo_reward(update: Update, context: CallbackContext):
    try:
        reward = float(update.message.text)
        context.user_data['promo_reward'] = reward
        update.message.reply_text("🔢 **Max usage** (-1 unlimited):")
        return PROMO_LIMIT
    except:
        update.message.reply_text("❌ Invalid number")
        return PROMO_REWARD

def add_promo_limit(update: Update, context: CallbackContext):
    try:
        limit = int(update.message.text)
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO promos (code, reward, max_usage, expiry_date) VALUES (?,?,?,?)",
                  (context.user_data['promo_code'], context.user_data['promo_reward'], limit, "2099-12-31"))
        conn.commit()
        conn.close()
        update.message.reply_text(f"✅ Promo created")
        return ConversationHandler.END
    except:
        update.message.reply_text("❌ Invalid number")
        return PROMO_LIMIT

def admin_list_promos_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM promos")
    promos = c.fetchall()
    conn.close()
    
    if not promos:
        query.edit_message_text("📭 No promos")
        return
    
    for p in promos:
        text = f"Code: `{p['code']}`\nReward: {p['reward']}\nUsed: {p['used_count']}/{p['max_usage']}\nExpiry: {p['expiry_date']}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🗑️ Delete", callback_data=f"admin_del_promo_{p['code']}")]])
        context.bot.send_message(query.from_user.id, text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

def admin_del_promo_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    code = query.data.split("_")[3]
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM promos WHERE code=?", (code,))
    conn.commit()
    conn.close()
    query.edit_message_text("🗑️ Promo deleted")

def admin_tasks_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Create", callback_data="admin_create_task")],
        [InlineKeyboardButton("📋 List", callback_data="admin_list_tasks")]
    ])
    query.edit_message_text("📋 **Tasks**", reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

def admin_create_task_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    query.message.reply_text("📝 **Description:**")
    return TASK_DESC

def add_task_desc(update: Update, context: CallbackContext):
    context.user_data['task_desc'] = update.message.text
    update.message.reply_text("🔗 **Link** (or None):")
    return TASK_LINK

def add_task_link(update: Update, context: CallbackContext):
    context.user_data['task_link'] = update.message.text
    update.message.reply_text("💰 **Reward:**")
    return TASK_REWARD

def add_task_reward(update: Update, context: CallbackContext):
    try:
        reward = float(update.message.text)
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("INSERT INTO tasks (description, link, reward) VALUES (?,?,?)",
                  (context.user_data['task_desc'], context.user_data['task_link'], reward))
        conn.commit()
        conn.close()
        update.message.reply_text("✅ Task created")
        return ConversationHandler.END
    except:
        update.message.reply_text("❌ Invalid number")
        return TASK_REWARD

def admin_list_tasks_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM tasks")
    tasks = c.fetchall()
    conn.close()
    
    if not tasks:
        query.edit_message_text("📭 No tasks")
        return
    
    for t in tasks:
        text = f"ID: {t['id']}\nDesc: {t['description']}\nLink: {t['link']}\nReward: {t['reward']}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🗑️ Delete", callback_data=f"admin_del_task_{t['id']}")]])
        context.bot.send_message(query.from_user.id, text, reply_markup=kb)

def admin_del_task_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    task_id = int(query.data.split("_")[3])
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    conn.commit()
    conn.close()
    query.edit_message_text("🗑️ Task deleted")

def admin_orders_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT orders.*, products.name FROM orders JOIN products ON orders.product_id=products.id WHERE status='pending'")
    orders = c.fetchall()
    conn.close()
    
    if not orders:
        query.edit_message_text("📭 No pending orders")
        return
    
    for o in orders:
        text = f"📦 Order #{o['id']}\nProduct: {o['name']}\nUser: {o['user_id']}\nData: {o['data']}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Deliver", callback_data=f"admin_deliver_order_{o['id']}")]])
        context.bot.send_message(query.from_user.id, text, reply_markup=kb)

def admin_deliver_order_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    order_id = int(query.data.split("_")[4])
    
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE orders SET status='delivered' WHERE id=?", (order_id,))
    conn.commit()
    conn.close()
    
    query.edit_message_text(f"✅ Order #{order_id} delivered")

def back_to_admin_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    query.edit_message_text("🔧 **Admin Panel**", reply_markup=admin_panel_kb(), parse_mode=ParseMode.MARKDOWN)

def back_to_cat_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    cat_id = int(query.data.split("_")[3])
    
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM products WHERE category_id=?", (cat_id,))
    prods = c.fetchall()
    conn.close()
    
    kb = []
    for p in prods:
        stock = "∞" if p['stock'] == -1 else p['stock']
        kb.append([InlineKeyboardButton(f"{p['name']} | {p['price']} | Stock:{stock}", callback_data=f"prod_{p['id']}")])
    
    query.edit_message_text("📦 **Products:**", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

# ---------- Cancel ----------
def cancel(update: Update, context: CallbackContext):
    update.message.reply_text("❌ Cancelled")
    return ConversationHandler.END

# ---------- Main ----------
def main():
    init_db()
    
    # Start Flask in thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # Create updater
    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher
    
    # Conversation Handlers
    start_conv = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={CAPTCHA: [MessageHandler(Filters.text & ~Filters.command, captcha_handler)]},
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    promo_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(promo_ask, pattern='^redeem_promo$')],
        states={PROMO_CODE: [MessageHandler(Filters.text & ~Filters.command, promo_redeem)]},
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    address_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(buy_cb, pattern='^buy_')],
        states={ADDRESS: [MessageHandler(Filters.text & ~Filters.command, address_handler)]},
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    # Admin conversations
    admin_cat_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_add_cat_cb, pattern='^admin_add_cat$')],
        states={CATEGORY_NAME: [MessageHandler(Filters.text & ~Filters.command, add_category)]},
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    admin_edit_cat_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_edit_cat_cb, pattern='^admin_edit_cat_')],
        states={CATEGORY_EDIT: [MessageHandler(Filters.text & ~Filters.command, edit_category)]},
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    admin_prod_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_add_prod_cb, pattern='^admin_add_prod_cat_')],
        states={
            PRODUCT_NAME: [MessageHandler(Filters.text & ~Filters.command, add_product_name)],
            PRODUCT_DESC: [MessageHandler(Filters.text & ~Filters.command, add_product_desc)],
            PRODUCT_PRICE: [MessageHandler(Filters.text & ~Filters.command, add_product_price)],
            PRODUCT_TYPE: [MessageHandler(Filters.text & ~Filters.command, add_product_type)],
            PRODUCT_CONTENT: [MessageHandler(Filters.all, add_product_content)],
            PRODUCT_STOCK: [MessageHandler(Filters.text & ~Filters.command, add_product_stock)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    admin_edit_prod_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_edit_prod_cb, pattern='^admin_edit_prod_')],
        states={
            PRODUCT_TYPE: [CallbackQueryHandler(admin_edit_field_cb, pattern='^edit_')],
            PRODUCT_NAME: [MessageHandler(Filters.text & ~Filters.command, admin_update_product)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    admin_user_search_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_search_cb, pattern='^admin_search_user$')],
        states={USER_SEARCH: [MessageHandler(Filters.text & ~Filters.command, admin_show_user)]},
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    admin_balance_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_give_bal_cb, pattern='^admin_give_bal_')],
        states={BALANCE_CHANGE: [MessageHandler(Filters.text & ~Filters.command, admin_update_balance)]},
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    admin_setting_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_setting_cb, pattern='^set_')],
        states={SETTING_VALUE: [MessageHandler(Filters.text & ~Filters.command, admin_save_setting)]},
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    admin_promo_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_create_promo_cb, pattern='^admin_create_promo$')],
        states={
            PROMO_CODE: [MessageHandler(Filters.text & ~Filters.command, add_promo_code)],
            PROMO_REWARD: [MessageHandler(Filters.text & ~Filters.command, add_promo_reward)],
            PROMO_LIMIT: [MessageHandler(Filters.text & ~Filters.command, add_promo_limit)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    admin_task_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_create_task_cb, pattern='^admin_create_task$')],
        states={
            TASK_DESC: [MessageHandler(Filters.text & ~Filters.command, add_task_desc)],
            TASK_LINK: [MessageHandler(Filters.text & ~Filters.command, add_task_link)],
            TASK_REWARD: [MessageHandler(Filters.text & ~Filters.command, add_task_reward)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    # Add handlers
    dp.add_handler(start_conv)
    dp.add_handler(promo_conv)
    dp.add_handler(address_conv)
    dp.add_handler(admin_cat_conv)
    dp.add_handler(admin_edit_cat_conv)
    dp.add_handler(admin_prod_conv)
    dp.add_handler(admin_edit_prod_conv)
    dp.add_handler(admin_user_search_conv)
    dp.add_handler(admin_balance_conv)
    dp.add_handler(admin_setting_conv)
    dp.add_handler(admin_promo_conv)
    dp.add_handler(admin_task_conv)
    
    # Message handlers
    dp.add_handler(MessageHandler(Filters.text & Filters.regex('^👤 Profile$'), profile))
    dp.add_handler(MessageHandler(Filters.text & Filters.regex('^🎁 Daily Bonus$'), daily_bonus))
    dp.add_handler(MessageHandler(Filters.text & Filters.regex('^🎲 Scratch Card$'), scratch_card))
    dp.add_handler(MessageHandler(Filters.text & Filters.regex('^📋 Tasks$'), tasks_list))
    dp.add_handler(MessageHandler(Filters.text & Filters.regex('^ℹ️ Support$'), support))
    dp.add_handler(MessageHandler(Filters.text & Filters.regex('^📜 Rules$'), rules))
    dp.add_handler(MessageHandler(Filters.text & Filters.regex('^🛍 Shop$'), shop))
    dp.add_handler(MessageHandler(Filters.text & Filters.regex('^⚙️ Admin Panel$'), admin_panel))
    
    # Callback query handlers
    dp.add_handler(CallbackQueryHandler(my_transactions_cb, pattern='^my_transactions$'))
    dp.add_handler(CallbackQueryHandler(my_orders_cb, pattern='^my_orders$'))
    dp.add_handler(CallbackQueryHandler(do_task_cb, pattern='^do_task_'))
    dp.add_handler(CallbackQueryHandler(category_cb, pattern='^cat_'))
    dp.add_handler(CallbackQueryHandler(product_cb, pattern='^prod_'))
    dp.add_handler(CallbackQueryHandler(back_to_cat_cb, pattern='^back_to_cat_'))
    
    dp.add_handler(CallbackQueryHandler(admin_stats_cb, pattern='^admin_stats$'))
    dp.add_handler(CallbackQueryHandler(admin_backup_cb, pattern='^admin_backup$'))
    dp.add_handler(CallbackQueryHandler(admin_broadcast_cb, pattern='^admin_broadcast$'))
    dp.add_handler(CallbackQueryHandler(admin_shop_cb, pattern='^admin_shop$'))
    dp.add_handler(CallbackQueryHandler(admin_list_cats_cb, pattern='^admin_list_cats$'))
    dp.add_handler(CallbackQueryHandler(admin_cat_cb, pattern='^admin_cat_'))
    dp.add_handler(CallbackQueryHandler(admin_del_cat_cb, pattern='^admin_del_cat_'))
    dp.add_handler(CallbackQueryHandler(admin_del_prod_cb, pattern='^admin_del_prod_'))
    dp.add_handler(CallbackQueryHandler(admin_users_cb, pattern='^admin_users$'))
    dp.add_handler(CallbackQueryHandler(admin_list_users_cb, pattern='^admin_list_users$'))
    dp.add_handler(CallbackQueryHandler(admin_ban_cb, pattern='^admin_ban_'))
    dp.add_handler(CallbackQueryHandler(admin_settings_cb, pattern='^admin_settings$'))
    dp.add_handler(CallbackQueryHandler(admin_promos_cb, pattern='^admin_promos$'))
    dp.add_handler(CallbackQueryHandler(admin_list_promos_cb, pattern='^admin_list_promos$'))
    dp.add_handler(CallbackQueryHandler(admin_del_promo_cb, pattern='^admin_del_promo_'))
    dp.add_handler(CallbackQueryHandler(admin_tasks_cb, pattern='^admin_tasks$'))
    dp.add_handler(CallbackQueryHandler(admin_list_tasks_cb, pattern='^admin_list_tasks$'))
    dp.add_handler(CallbackQueryHandler(admin_del_task_cb, pattern='^admin_del_task_'))
    dp.add_handler(CallbackQueryHandler(admin_orders_cb, pattern='^admin_orders$'))
    dp.add_handler(CallbackQueryHandler(admin_deliver_order_cb, pattern='^admin_deliver_order_'))
    dp.add_handler(CallbackQueryHandler(back_to_admin_cb, pattern='^admin_panel$'))
    
    # Start bot
    updater.start_polling()
    print(f"🤖 Bot running on port {PORT}")
    updater.idle()

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    main()
