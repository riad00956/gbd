import os
import sqlite3
import random
import threading
import logging
import re
import time
from datetime import datetime
from functools import wraps
from flask import Flask, jsonify
import telebot
from telebot import types
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

# ---------- Configuration ----------
TOKEN = os.getenv("BT")
ADMIN_ID = os.getenv("AD")
DB_NAME = "shop_bot.db"
PORT = int(os.environ.get("PORT", 1000))

# ---------- Markdown Escape ----------
def escape_markdown(text):
    """Escape Markdown special characters in user input."""
    if text is None:
        return ""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', str(text))

# ---------- Flask Health Check ----------
app = Flask(__name__)

@app.route('/')
def health():
    return jsonify({"status": "Bot is running", "uptime": str(datetime.now())}), 200

def run_flask():
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

# ---------- Bot Initialization ----------
bot = telebot.TeleBot(TOKEN, parse_mode="Markdown")

# Temporary data storage for multi-step operations
temp_data = {}

# ---------- Database Helpers (SQLite with timeout) ----------
def get_db_connection():
    """Return a thread-safe SQLite connection with timeout."""
    return sqlite3.connect(DB_NAME, timeout=10, check_same_thread=False)

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    # Settings table
    c.execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT
    )""")
    defaults = {
        "welcome_message": "Welcome to our Premium Shop, {name}!",
        "currency": "★",
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
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, val))

    # Users table
    c.execute("""CREATE TABLE IF NOT EXISTS users (
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
    )""")

    # Transactions table
    c.execute("""CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount REAL,
        type TEXT,
        description TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    # Categories table
    c.execute("""CREATE TABLE IF NOT EXISTS categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE
    )""")

    # Products table
    c.execute("""CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category_id INTEGER,
        type TEXT,
        name TEXT,
        description TEXT,
        price REAL,
        content TEXT,
        stock INTEGER DEFAULT -1,
        FOREIGN KEY(category_id) REFERENCES categories(id)
    )""")

    # Orders table
    c.execute("""CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        product_id INTEGER,
        status TEXT,
        data TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    # Tasks table
    c.execute("""CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        description TEXT,
        link TEXT,
        reward REAL
    )""")

    # Completed tasks table
    c.execute("""CREATE TABLE IF NOT EXISTS completed_tasks (
        user_id INTEGER,
        task_id INTEGER,
        PRIMARY KEY (user_id, task_id)
    )""")

    # Promos table
    c.execute("""CREATE TABLE IF NOT EXISTS promos (
        code TEXT PRIMARY KEY,
        reward REAL,
        max_usage INTEGER,
        used_count INTEGER DEFAULT 0,
        expiry_date TEXT
    )""")

    # Promo usage table
    c.execute("""CREATE TABLE IF NOT EXISTS promo_usage (
        user_id INTEGER,
        code TEXT,
        PRIMARY KEY (user_id, code)
    )""")

    conn.commit()
    conn.close()

def get_setting(key):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else ""

def update_setting(key, value):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()

def get_user(user_id):
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row

def add_transaction(user_id, amount, ttype, description):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO transactions (user_id, amount, type, description) VALUES (?, ?, ?, ?)",
              (user_id, amount, ttype, description))
    conn.commit()
    conn.close()

def get_currency():
    return get_setting("currency") or "★"

# ---------- Middleware ----------
def check_force_join(user_id):
    channel = get_setting("channel_force_join")
    if not channel:
        return True
    try:
        member = bot.get_chat_member(channel, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except:
        return True

def force_join_required(func):
    @wraps(func)
    def wrapper(message):
        if not check_force_join(message.from_user.id):
            channel = get_setting("channel_force_join")
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("Join Channel", url=f"https://t.me/{channel.replace('@','')}"))
            markup.add(InlineKeyboardButton("✅ Joined", callback_data="check_joined"))
            bot.reply_to(message, f"⚠️ You must join our channel {channel} to use this bot.", reply_markup=markup)
            return
        return func(message)
    return wrapper

def admin_only(func):
    @wraps(func)
    def wrapper(message):
        if str(message.from_user.id) != str(ADMIN_ID):
            bot.reply_to(message, "⛔ You are not authorized.")
            return
        return func(message)
    return wrapper

def admin_only_callback(func):
    @wraps(func)
    def wrapper(call):
        if str(call.from_user.id) != str(ADMIN_ID):
            bot.answer_callback_query(call.id, "⛔ Unauthorized", show_alert=True)
            return
        return func(call)
    return wrapper

# ---------- Keyboards ----------
def main_menu_kb(is_admin=False):
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = [
        KeyboardButton("🛍️ Shop"),
        KeyboardButton("👤 Profile"),
        KeyboardButton("🎁 Daily Bonus"),
        KeyboardButton("✨ Scratch Card"),
        KeyboardButton("📋 Tasks"),
        KeyboardButton("ℹ️ Support"),
        KeyboardButton("📜 Rules")
    ]
    if is_admin:
        buttons.append(KeyboardButton("⚙️ Admin Panel"))
    markup.add(*buttons)
    return markup

def admin_panel_kb():
    markup = InlineKeyboardMarkup(row_width=2)
    buttons = [
        InlineKeyboardButton("📊 Stats", callback_data="admin_stats"),
        InlineKeyboardButton("👥 Users", callback_data="admin_users"),
        InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"),
        InlineKeyboardButton("🛍 Shop Mgmt", callback_data="admin_shop"),
        InlineKeyboardButton("📦 Orders", callback_data="admin_orders"),
        InlineKeyboardButton("⚙️ Settings", callback_data="admin_settings"),
        InlineKeyboardButton("🎁 Promos", callback_data="admin_promos"),
        InlineKeyboardButton("📋 Tasks Mgmt", callback_data="admin_tasks"),
        InlineKeyboardButton("🗄️ Backup DB", callback_data="admin_backup"),
        InlineKeyboardButton("🏆 Leaderboard", callback_data="admin_leaderboard")
    ]
    markup.add(*buttons)
    return markup

# ---------- Start & Captcha ----------
@bot.message_handler(commands=['start'])
def cmd_start(message):
    user_id = message.from_user.id
    args = message.text.split()
    referrer_id = None
    if len(args) > 1 and args[1].isdigit():
        ref = int(args[1])
        if ref != user_id:
            referrer_id = ref

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        user = c.fetchone()
        if not user:
            c.execute("""INSERT INTO users (user_id, username, full_name, referrer_id)
                         VALUES (?, ?, ?, ?)""",
                      (user_id, message.from_user.username, message.from_user.full_name, referrer_id))
            if referrer_id:
                reward = float(get_setting("referral_reward"))
                curr = get_currency()
                c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (reward, referrer_id))
                add_transaction(referrer_id, reward, "credit", f"Referral bonus for {user_id}")
                try:
                    bot.send_message(referrer_id, f"🎉 Someone joined via your link! You got {reward} {curr}")
                except:
                    pass
            conn.commit()
    except sqlite3.OperationalError as e:
        print(f"Database error in start: {e}")
        bot.reply_to(message, "⚠️ System busy, please try again.")
        return
    finally:
        if conn:
            conn.close()

    if not check_force_join(user_id):
        channel = get_setting("channel_force_join")
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("Join Channel", url=f"https://t.me/{channel.replace('@','')}"))
        markup.add(InlineKeyboardButton("✅ Joined", callback_data="check_joined"))
        bot.reply_to(message, f"⚠️ You must join our channel {channel} to use this bot.", reply_markup=markup)
        return

    if get_setting("captcha_enabled") == "1":
        emoji = random.choice(["🐱", "🐶", "🦊", "🐯", "🐼"])
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton(emoji, callback_data="captcha_ok"))
        bot.reply_to(message, f"🔒 Security: Click the {emoji} emoji below:", reply_markup=markup)
        return

    show_welcome(message)

@bot.callback_query_handler(func=lambda call: call.data == "check_joined")
def check_joined_callback(call):
    if check_force_join(call.from_user.id):
        bot.delete_message(call.message.chat.id, call.message.message_id)
        show_welcome(call.message)
    else:
        bot.answer_callback_query(call.id, "❌ You haven't joined yet!", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data == "captcha_ok")
def captcha_callback(call):
    bot.delete_message(call.message.chat.id, call.message.message_id)
    show_welcome(call.message)
    bot.answer_callback_query(call.id)

def show_welcome(message):
    user_id = message.from_user.id
    user = get_user(user_id)
    full_name = escape_markdown(user['full_name']) if user else escape_markdown(message.from_user.full_name)
    welcome = get_setting("welcome_message").replace("{name}", full_name)
    is_admin = str(user_id) == str(ADMIN_ID)
    bot.send_message(message.chat.id, welcome, reply_markup=main_menu_kb(is_admin))

# ---------- Profile ----------
@bot.message_handler(func=lambda m: m.text == "👤 Profile")
@force_join_required
def profile_handler(message):
    user_id = message.from_user.id
    user = get_user(user_id)
    if not user:
        bot.reply_to(message, "Please /start first.")
        return
    curr = get_currency()
    bot_info = bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={user_id}"

    text = (f"👤 *Profile Details*\n\n"
            f"🆔 ID: `{user_id}`\n"
            f"💰 Points: `{user['balance']:.2f} {curr}`\n"
            f"💸 Total Spent: `{user['total_spent']:.2f} {curr}`\n"
            f"📅 Joined: {escape_markdown(user['joined_at'])}\n\n"
            f"🔗 *Referral Link:*\n`{ref_link}`")

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🎁 Redeem Promo", callback_data="redeem_promo"))
    markup.add(InlineKeyboardButton("📜 Transactions", callback_data="my_txs"))
    bot.send_message(message.chat.id, text, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "my_txs")
def my_transactions(call):
    try:
        user_id = call.from_user.id
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM transactions WHERE user_id = ? ORDER BY timestamp DESC LIMIT 10", (user_id,))
        rows = c.fetchall()
        conn.close()
        if not rows:
            bot.send_message(call.message.chat.id, "No transactions yet.")
            return
        text = "📜 *Last 10 Transactions*\n\n"
        for r in rows:
            sign = "+" if r['amount'] > 0 else ""
            desc = escape_markdown(r['description'])
            text += f"{r['timestamp'][:10]} {r['type']}: {sign}{r['amount']} - {desc}\n"
        bot.send_message(call.message.chat.id, text)
        bot.answer_callback_query(call.id)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error: {str(e)}", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data == "redeem_promo")
def redeem_promo_start(call):
    try:
        msg = bot.send_message(call.message.chat.id, "🎟️ Send me the promo code:")
        bot.register_next_step_handler(msg, process_redeem_promo)
        bot.answer_callback_query(call.id)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error: {str(e)}", show_alert=True)

def process_redeem_promo(message):
    code = message.text.strip().upper()
    user_id = message.from_user.id
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM promos WHERE code = ?", (code,))
    promo = c.fetchone()
    if not promo:
        bot.reply_to(message, "❌ Invalid promo code.")
        conn.close()
        return
    if promo[4]:
        exp_date = datetime.fromisoformat(promo[4])
        if datetime.now() > exp_date:
            bot.reply_to(message, "❌ This promo has expired.")
            conn.close()
            return
    if promo[2] != -1 and promo[3] >= promo[2]:
        bot.reply_to(message, "❌ This promo has reached its usage limit.")
        conn.close()
        return
    c.execute("SELECT * FROM promo_usage WHERE user_id = ? AND code = ?", (user_id, code))
    if c.fetchone():
        bot.reply_to(message, "❌ You have already used this promo.")
        conn.close()
        return

    reward = promo[1]
    c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (reward, user_id))
    c.execute("UPDATE promos SET used_count = used_count + 1 WHERE code = ?", (code,))
    c.execute("INSERT INTO promo_usage (user_id, code) VALUES (?, ?)", (user_id, code))
    conn.commit()
    add_transaction(user_id, reward, "credit", f"Promo code: {code}")
    bot.reply_to(message, f"✅ Promo applied! You received {reward} {get_currency()}.")
    conn.close()

# ---------- Daily Bonus ----------
@bot.message_handler(func=lambda m: m.text == "🎁 Daily Bonus")
@force_join_required
def daily_bonus(message):
    try:
        if get_setting("daily_enabled") == "0":
            bot.reply_to(message, "Daily bonus is currently disabled.")
            return
        user_id = message.from_user.id
        user = get_user(user_id)
        if not user:
            bot.reply_to(message, "Please /start first.")
            return
        now = datetime.now()
        if user['last_daily_claim']:
            last = datetime.fromisoformat(user['last_daily_claim'])
            if (now - last).total_seconds() < 86400:
                bot.reply_to(message, "❌ You already claimed today. Come back later!")
                return
        reward = float(get_setting("daily_reward"))
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("UPDATE users SET balance = balance + ?, last_daily_claim = ? WHERE user_id = ?",
                  (reward, now.isoformat(), user_id))
        conn.commit()
        conn.close()
        add_transaction(user_id, reward, "credit", "Daily Bonus")
        bot.reply_to(message, f"🎉 You received {reward} {get_currency()} as daily bonus!")
    except Exception as e:
        bot.reply_to(message, f"Error: {str(e)}")

# ---------- Scratch Card ----------
@bot.message_handler(func=lambda m: m.text == "✨ Scratch Card")
@force_join_required
def scratch_card(message):
    try:
        if get_setting("scratch_enabled") == "0":
            bot.reply_to(message, "Scratch card is currently disabled.")
            return
        user_id = message.from_user.id
        user = get_user(user_id)
        if not user:
            bot.reply_to(message, "Please /start first.")
            return
        now = datetime.now()
        if user['last_scratch_claim']:
            last = datetime.fromisoformat(user['last_scratch_claim'])
            if (now - last).total_seconds() < 86400:
                bot.reply_to(message, "❌ You already scratched a card today!")
                return
        rewards = [float(x) for x in get_setting("scratch_rewards").split(",")]
        win = random.choice(rewards)
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("UPDATE users SET balance = balance + ?, last_scratch_claim = ? WHERE user_id = ?",
                  (win, now.isoformat(), user_id))
        conn.commit()
        conn.close()
        add_transaction(user_id, win, "credit", "Scratch Card Win")
        bot.reply_to(message, f"✨ You scratched the card and won `{win:.2f}` {get_currency()}!")
    except Exception as e:
        bot.reply_to(message, f"Error: {str(e)}")

# ---------- Tasks ----------
@bot.message_handler(func=lambda m: m.text == "📋 Tasks")
@force_join_required
def list_tasks(message):
    try:
        user_id = message.from_user.id
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM tasks")
        tasks = c.fetchall()
        if not tasks:
            bot.reply_to(message, "No tasks available at the moment.")
            conn.close()
            return
        c.execute("SELECT task_id FROM completed_tasks WHERE user_id = ?", (user_id,))
        completed = {row[0] for row in c.fetchall()}
        conn.close()

        markup = InlineKeyboardMarkup()
        for task in tasks:
            if task['id'] not in completed:
                desc = escape_markdown(task['description'])
                markup.add(InlineKeyboardButton(f"✅ {desc} (+{task['reward']})", callback_data=f"task_{task['id']}"))
        if not markup.keyboard:
            bot.send_message(message.chat.id, "You have completed all tasks! Check back later.")
            return
        bot.send_message(message.chat.id, "📋 *Available Tasks*\nClick a task to complete it:", reply_markup=markup)
    except Exception as e:
        bot.reply_to(message, f"Error: {str(e)}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("task_"))
def task_callback(call):
    try:
        task_id = int(call.data.split("_")[1])
        user_id = call.from_user.id
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        task = c.fetchone()
        if not task:
            bot.answer_callback_query(call.id, "Task not found.")
            conn.close()
            return
        c.execute("SELECT * FROM completed_tasks WHERE user_id = ? AND task_id = ?", (user_id, task_id))
        if c.fetchone():
            bot.answer_callback_query(call.id, "You already completed this task.")
            conn.close()
            return
        c.execute("INSERT INTO completed_tasks (user_id, task_id) VALUES (?, ?)", (user_id, task_id))
        c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (task['reward'], user_id))
        conn.commit()
        add_transaction(user_id, task['reward'], "credit", f"Task: {task['description']}")
        bot.answer_callback_query(call.id, f"✅ Task completed! You earned {task['reward']} {get_currency()}.")
        bot.send_message(call.message.chat.id, f"🎉 You completed: {escape_markdown(task['description'])}\n💰 Reward: {task['reward']} {get_currency()}")
        conn.close()
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error: {str(e)}", show_alert=True)

# ---------- Support & Rules ----------
@bot.message_handler(func=lambda m: m.text == "ℹ️ Support")
def support(message):
    link = get_setting("support_link")
    bot.reply_to(message, f"📞 For support, contact: {link}")

@bot.message_handler(func=lambda m: m.text == "📜 Rules")
def rules(message):
    rules_text = escape_markdown(get_setting("rules"))
    bot.reply_to(message, f"📜 *Rules:*\n{rules_text}")

# ---------- Shop ----------
@bot.message_handler(func=lambda m: m.text == "🛍️ Shop")
@force_join_required
def shop_entry(message):
    try:
        if get_setting("shop_enabled") == "0":
            bot.reply_to(message, "⚠️ Shop is currently disabled.")
            return
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM categories")
        cats = c.fetchall()
        conn.close()
        if not cats:
            bot.reply_to(message, "No categories available.")
            return
        markup = InlineKeyboardMarkup(row_width=2)
        for cat in cats:
            markup.add(InlineKeyboardButton(escape_markdown(cat['name']), callback_data=f"cat_{cat['id']}"))
        bot.send_message(message.chat.id, "📂 Select a category:", reply_markup=markup)
    except Exception as e:
        bot.reply_to(message, f"Error: {str(e)}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("cat_"))
def show_products(call):
    try:
        cat_id = int(call.data.split("_")[1])
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM products WHERE category_id = ?", (cat_id,))
        prods = c.fetchall()
        conn.close()
        if not prods:
            bot.answer_callback_query(call.id, "No products in this category.")
            return
        markup = InlineKeyboardMarkup(row_width=1)
        curr = get_currency()
        for p in prods:
            stock = "∞" if p['stock'] == -1 else p['stock']
            name = escape_markdown(p['name'])
            markup.add(InlineKeyboardButton(f"{name} | {p['price']} {curr} | Stock: {stock}",
                                             callback_data=f"prod_{p['id']}"))
        markup.add(InlineKeyboardButton("🔙 Back", callback_data="shop_main"))
        bot.edit_message_text("📦 Available Products:", call.message.chat.id, call.message.message_id, reply_markup=markup)
        bot.answer_callback_query(call.id)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error: {str(e)}", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith("prod_"))
def product_detail(call):
    try:
        prod_id = int(call.data.split("_")[1])
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM products WHERE id = ?", (prod_id,))
        p = c.fetchone()
        conn.close()
        if not p:
            bot.answer_callback_query(call.id, "Product not found.")
            return
        curr = get_currency()
        name = escape_markdown(p['name'])
        desc = escape_markdown(p['description'])
        text = (f"📦 *{name}*\n\n"
                f"📝 {desc}\n\n"
                f"💰 Price: `{p['price']} {curr}`\n"
                f"📊 Stock: `{'Unlimited' if p['stock'] == -1 else p['stock']}`")
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🛒 Buy Now", callback_data=f"buy_{p['id']}"))
        markup.add(InlineKeyboardButton("🔙 Back", callback_data=f"cat_{p['category_id']}"))
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup)
        bot.answer_callback_query(call.id)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error: {str(e)}", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith("buy_"))
def buy_product(call):
    try:
        prod_id = int(call.data.split("_")[1])
        user_id = call.from_user.id
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM products WHERE id = ?", (prod_id,))
        p = c.fetchone()
        c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        u = c.fetchone()
        if not p or not u:
            bot.answer_callback_query(call.id, "Error: product or user not found.")
            conn.close()
            return
        if u['balance'] < p['price']:
            bot.answer_callback_query(call.id, "❌ Insufficient points!", show_alert=True)
            conn.close()
            return
        if p['stock'] != -1 and p['stock'] <= 0:
            bot.answer_callback_query(call.id, "❌ Out of stock!", show_alert=True)
            conn.close()
            return

        c.execute("UPDATE users SET balance = balance - ?, total_spent = total_spent + ? WHERE user_id = ?",
                  (p['price'], p['price'], user_id))
        if p['stock'] != -1:
            c.execute("UPDATE products SET stock = stock - 1 WHERE id = ?", (prod_id,))

        if p['type'] == 'digital':
            status = "delivered"
            delivery_data = p['content']
        elif p['type'] == 'file':
            status = "delivered"
            delivery_data = p['content']
        else:
            status = "pending"
            delivery_data = "Manual fulfillment required"

        c.execute("INSERT INTO orders (user_id, product_id, status, data) VALUES (?, ?, ?, ?)",
                  (user_id, prod_id, status, delivery_data))
        conn.commit()
        add_transaction(user_id, -p['price'], "debit", f"Purchased {p['name']}")

        # Send delivery
        if p['type'] == 'digital':
            bot.send_message(user_id, f"✅ Purchase Successful!\n\nYour item:\n`{escape_markdown(delivery_data)}`")
        elif p['type'] == 'file':
            try:
                bot.send_document(user_id, delivery_data, caption=f"✅ Your purchased file: {escape_markdown(p['name'])}")
            except Exception as file_err:
                bot.send_message(user_id, f"✅ Purchase Successful! File ID: {delivery_data}\n(Error sending file: {file_err})")
        else:
            bot.send_message(user_id, "✅ Purchase Successful! Your order is pending admin approval. You'll receive it soon.")
        conn.close()
        bot.answer_callback_query(call.id)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error: {str(e)}", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data == "shop_main")
def shop_main(call):
    try:
        shop_entry(call.message)
        bot.answer_callback_query(call.id)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error: {str(e)}", show_alert=True)

# ---------- Admin Panel ----------
@bot.message_handler(func=lambda m: m.text == "⚙️ Admin Panel")
@admin_only
def admin_panel(message):
    bot.send_message(message.chat.id, "🔧 *Welcome to Admin Control*", reply_markup=admin_panel_kb())

# Debug commands
@bot.message_handler(commands=['get_setting'])
@admin_only
def get_setting_command(message):
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /get_setting <key>")
        return
    key = parts[1]
    value = get_setting(key)
    bot.reply_to(message, f"`{key}` = `{escape_markdown(value)}`")

@bot.message_handler(commands=['get_welcome'])
@admin_only
def get_welcome(message):
    welcome = get_setting("welcome_message")
    bot.reply_to(message, f"📢 *Current Welcome Message:*\n{escape_markdown(welcome)}", parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data == "admin_stats")
@admin_only_callback
def admin_stats(call):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM users")
        user_count = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM orders")
        order_count = c.fetchone()[0]
        c.execute("SELECT SUM(total_spent) FROM users")
        revenue = c.fetchone()[0] or 0
        conn.close()
        text = (f"📊 *Bot Statistics*\n\n"
                f"👥 Total Users: `{user_count}`\n"
                f"📦 Total Orders: `{order_count}`\n"
                f"💰 Total Revenue: `{revenue:.2f} {get_currency()}`")
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=admin_panel_kb())
        bot.answer_callback_query(call.id)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error: {str(e)}", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data == "admin_users")
@admin_only_callback
def admin_users(call):
    try:
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT user_id, username, balance, banned FROM users LIMIT 10")
        users = c.fetchall()
        conn.close()
        text = "👥 *Recent Users*\n\n"
        for u in users:
            status = "🔴 Banned" if u['banned'] else "🟢 Active"
            username = escape_markdown(u['username']) if u['username'] else "None"
            text += f"🆔 {u['user_id']} | @{username} | {u['balance']} {get_currency()} | {status}\n"
        text += "\nUse /search <id> to find a specific user."
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=admin_panel_kb())
        bot.answer_callback_query(call.id)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error: {str(e)}", show_alert=True)

@bot.message_handler(commands=['search'])
@admin_only
def search_user(message):
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "Usage: /search <user_id>")
            return
        user_id = parts[1]
        user = get_user(user_id)
        if not user:
            bot.reply_to(message, "User not found.")
            return
        curr = get_currency()
        text = (f"👤 *User Details*\n"
                f"ID: `{user['user_id']}`\n"
                f"Username: @{escape_markdown(user['username'])}\n"
                f"Name: {escape_markdown(user['full_name'])}\n"
                f"Points: {user['balance']} {curr}\n"
                f"Spent: {user['total_spent']} {curr}\n"
                f"Banned: {user['banned']}\n"
                f"Joined: {escape_markdown(user['joined_at'])}")
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("➕ Add Points", callback_data=f"addbal_{user_id}"))
        markup.add(InlineKeyboardButton("🔨 Ban/Unban", callback_data=f"ban_{user_id}"))
        bot.send_message(message.chat.id, text, reply_markup=markup)
    except Exception as e:
        bot.reply_to(message, f"Error: {str(e)}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("addbal_"))
@admin_only_callback
def add_balance_start(call):
    try:
        user_id = call.data.split("_")[1]
        msg = bot.send_message(call.message.chat.id, f"Enter amount to add to user {user_id}:")
        bot.register_next_step_handler(msg, process_add_balance, user_id)
        bot.answer_callback_query(call.id)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error: {str(e)}", show_alert=True)

def process_add_balance(message, target_id):
    try:
        amount = float(message.text)
    except:
        bot.reply_to(message, "Invalid amount.")
        return
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, target_id))
    conn.commit()
    add_transaction(target_id, amount, "credit", "Admin add")
    bot.reply_to(message, f"✅ Added {amount} {get_currency()} to user {target_id}.")
    conn.close()

@bot.callback_query_handler(func=lambda call: call.data.startswith("ban_"))
@admin_only_callback
def toggle_ban(call):
    try:
        user_id = int(call.data.split("_")[1])
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT banned FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        if row:
            new_status = 0 if row[0] else 1
            c.execute("UPDATE users SET banned = ? WHERE user_id = ?", (new_status, user_id))
            conn.commit()
            bot.answer_callback_query(call.id, f"User {'banned' if new_status else 'unbanned'}.")
        conn.close()
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error: {str(e)}", show_alert=True)

# --- Broadcast ---
@bot.callback_query_handler(func=lambda call: call.data == "admin_broadcast")
@admin_only_callback
def broadcast_start(call):
    try:
        msg = bot.send_message(call.message.chat.id, "📢 Send the message (text/photo/video) you want to broadcast:")
        bot.register_next_step_handler(msg, process_broadcast)
        bot.answer_callback_query(call.id)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error: {str(e)}", show_alert=True)

def process_broadcast(message):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id FROM users")
    users = [row[0] for row in c.fetchall()]
    conn.close()
    sent = 0
    for uid in users:
        try:
            if message.content_type == 'text':
                bot.send_message(uid, message.text)
            elif message.content_type == 'photo':
                bot.send_photo(uid, message.photo[-1].file_id, caption=message.caption)
            elif message.content_type == 'video':
                bot.send_video(uid, message.video.file_id, caption=message.caption)
            else:
                continue
            sent += 1
        except:
            pass
    bot.reply_to(message, f"✅ Broadcast sent to {sent} users.")

# --- Shop Management ---
@bot.callback_query_handler(func=lambda call: call.data == "admin_shop")
@admin_only_callback
def admin_shop(call):
    try:
        markup = InlineKeyboardMarkup(row_width=2)
        buttons = [
            InlineKeyboardButton("➕ Add Category", callback_data="add_cat"),
            InlineKeyboardButton("➕ Add Product", callback_data="add_prod"),
            InlineKeyboardButton("🗑 Delete Category", callback_data="del_cat_list"),
            InlineKeyboardButton("🗑 Delete Product", callback_data="del_prod_list"),
            InlineKeyboardButton("🔙 Back", callback_data="admin_panel")
        ]
        markup.add(*buttons)
        bot.edit_message_text("🛍 *Shop Management*", call.message.chat.id, call.message.message_id, reply_markup=markup)
        bot.answer_callback_query(call.id)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error: {str(e)}", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data == "add_cat")
@admin_only_callback
def add_cat_start(call):
    try:
        msg = bot.send_message(call.message.chat.id, "📝 Enter new category name:")
        bot.register_next_step_handler(msg, add_cat_finish)
        bot.answer_callback_query(call.id)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error: {str(e)}", show_alert=True)

def add_cat_finish(message):
    name = message.text.strip()
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("INSERT INTO categories (name) VALUES (?)", (name,))
        conn.commit()
        bot.reply_to(message, f"✅ Category '{escape_markdown(name)}' added.")
    except sqlite3.IntegrityError:
        bot.reply_to(message, "❌ Category already exists.")
    conn.close()

@bot.callback_query_handler(func=lambda call: call.data == "del_cat_list")
@admin_only_callback
def del_cat_list(call):
    try:
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM categories")
        cats = c.fetchall()
        conn.close()
        if not cats:
            bot.answer_callback_query(call.id, "No categories.")
            return
        markup = InlineKeyboardMarkup()
        for cat in cats:
            name = escape_markdown(cat['name'])
            markup.add(InlineKeyboardButton(f"❌ {name}", callback_data=f"delcat_{cat['id']}"))
        bot.edit_message_text("Select category to delete:", call.message.chat.id, call.message.message_id, reply_markup=markup)
        bot.answer_callback_query(call.id)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error: {str(e)}", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith("delcat_"))
@admin_only_callback
def delete_category(call):
    try:
        cat_id = int(call.data.split("_")[1])
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
        conn.commit()
        conn.close()
        bot.answer_callback_query(call.id, "Category deleted.")
        del_cat_list(call)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error: {str(e)}", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data == "add_prod")
@admin_only_callback
def add_prod_start(call):
    try:
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM categories")
        cats = c.fetchall()
        conn.close()
        if not cats:
            bot.answer_callback_query(call.id, "No categories. Add one first.")
            return
        markup = InlineKeyboardMarkup()
        for cat in cats:
            name = escape_markdown(cat['name'])
            markup.add(InlineKeyboardButton(name, callback_data=f"selcat_{cat['id']}"))
        bot.edit_message_text("Select category for the new product:", call.message.chat.id, call.message.message_id, reply_markup=markup)
        bot.answer_callback_query(call.id)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error: {str(e)}", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith("selcat_"))
@admin_only_callback
def add_prod_category(call):
    try:
        cat_id = int(call.data.split("_")[1])
        user_id = call.from_user.id
        temp_data[user_id] = {'cat_id': cat_id}
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("Digital", callback_data="prodtype_digital"))
        markup.add(InlineKeyboardButton("File", callback_data="prodtype_file"))
        markup.add(InlineKeyboardButton("Manual", callback_data="prodtype_manual"))
        bot.edit_message_text("Select product type:", call.message.chat.id, call.message.message_id, reply_markup=markup)
        bot.answer_callback_query(call.id)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error: {str(e)}", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith("prodtype_"))
@admin_only_callback
def add_prod_type(call):
    try:
        prod_type = call.data.split("_")[1]
        user_id = call.from_user.id
        if user_id not in temp_data:
            temp_data[user_id] = {}
        temp_data[user_id]['type'] = prod_type
        msg = bot.send_message(call.message.chat.id, "Enter product name:")
        bot.register_next_step_handler(msg, add_prod_name)
        bot.answer_callback_query(call.id)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error: {str(e)}", show_alert=True)

def add_prod_name(message):
    user_id = message.from_user.id
    if user_id not in temp_data:
        bot.reply_to(message, "Session expired. Start over.")
        return
    temp_data[user_id]['name'] = message.text.strip()
    msg = bot.reply_to(message, "Enter product description:")
    bot.register_next_step_handler(msg, add_prod_desc)

def add_prod_desc(message):
    user_id = message.from_user.id
    if user_id not in temp_data:
        bot.reply_to(message, "Session expired.")
        return
    temp_data[user_id]['desc'] = message.text.strip()
    msg = bot.reply_to(message, "Enter price (number):")
    bot.register_next_step_handler(msg, add_prod_price)

def add_prod_price(message):
    try:
        price = float(message.text.strip())
    except:
        bot.reply_to(message, "Invalid price. Enter a number.")
        return
    user_id = message.from_user.id
    if user_id not in temp_data:
        bot.reply_to(message, "Session expired.")
        return
    temp_data[user_id]['price'] = price
    msg = bot.reply_to(message, "Enter stock (-1 for unlimited):")
    bot.register_next_step_handler(msg, add_prod_stock)

def add_prod_stock(message):
    try:
        stock = int(message.text.strip())
    except:
        bot.reply_to(message, "Invalid stock. Enter integer.")
        return
    user_id = message.from_user.id
    if user_id not in temp_data:
        bot.reply_to(message, "Session expired.")
        return
    temp_data[user_id]['stock'] = stock
    prod_type = temp_data[user_id]['type']
    if prod_type == "digital":
        msg = bot.reply_to(message, "Enter the digital content (text) to deliver:")
    elif prod_type == "file":
        msg = bot.reply_to(message, "Upload the file (will be saved as file_id):", parse_mode=None)
    else:
        msg = bot.reply_to(message, "Enter instructions for manual fulfillment:")
    bot.register_next_step_handler(msg, add_prod_content)

def add_prod_content(message):
    user_id = message.from_user.id
    if user_id not in temp_data:
        bot.reply_to(message, "Session expired.")
        return
    if temp_data[user_id]['type'] == "file":
        if message.content_type == 'document':
            content = message.document.file_id
        else:
            bot.reply_to(message, "Please upload a file.")
            return
    else:
        content = message.text.strip()

    data = temp_data[user_id]
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""INSERT INTO products (category_id, type, name, description, price, content, stock)
                 VALUES (?, ?, ?, ?, ?, ?, ?)""",
              (data['cat_id'], data['type'], data['name'], data['desc'], data['price'], content, data['stock']))
    conn.commit()
    conn.close()
    del temp_data[user_id]
    bot.reply_to(message, "✅ Product added successfully!")

# --- Orders ---
@bot.callback_query_handler(func=lambda call: call.data == "admin_orders")
@admin_only_callback
def admin_orders(call):
    try:
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM orders WHERE status='pending' ORDER BY created_at DESC LIMIT 10")
        orders = c.fetchall()
        conn.close()
        if not orders:
            bot.edit_message_text("No pending orders.", call.message.chat.id, call.message.message_id, reply_markup=admin_panel_kb())
            bot.answer_callback_query(call.id)
            return
        text = "📦 *Pending Orders*\n\n"
        markup = InlineKeyboardMarkup()
        for o in orders:
            text += f"Order #{o['id']} | User: {o['user_id']} | Product: {o['product_id']} | {o['created_at']}\n"
            markup.add(InlineKeyboardButton(f"Order #{o['id']}", callback_data=f"order_{o['id']}"))
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup)
        bot.answer_callback_query(call.id)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error: {str(e)}", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith("order_"))
@admin_only_callback
def order_detail(call):
    try:
        order_id = int(call.data.split("_")[1])
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
        o = c.fetchone()
        conn.close()
        if not o:
            bot.answer_callback_query(call.id, "Order not found.")
            return
        text = (f"📦 *Order #{o['id']}*\n"
                f"User: {o['user_id']}\n"
                f"Product: {o['product_id']}\n"
                f"Status: {o['status']}\n"
                f"Data: {escape_markdown(o['data'])}\n"
                f"Created: {o['created_at']}")
        markup = InlineKeyboardMarkup()
        if o['status'] == 'pending':
            markup.add(InlineKeyboardButton("✅ Mark Delivered", callback_data=f"markdelivered_{order_id}"))
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup)
        bot.answer_callback_query(call.id)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error: {str(e)}", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith("markdelivered_"))
@admin_only_callback
def mark_delivered(call):
    try:
        order_id = int(call.data.split("_")[1])
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("UPDATE orders SET status='delivered' WHERE id=?", (order_id,))
        conn.commit()
        conn.close()
        bot.answer_callback_query(call.id, "Order marked delivered.")
        order_detail(call)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error: {str(e)}", show_alert=True)

# --- Settings ---
@bot.callback_query_handler(func=lambda call: call.data == "admin_settings")
@admin_only_callback
def admin_settings(call):
    try:
        markup = InlineKeyboardMarkup(row_width=1)
        settings = [
            ("welcome_message", "Welcome Message"),
            ("currency", "Currency Symbol"),
            ("support_link", "Support Link"),
            ("rules", "Rules"),
            ("referral_reward", "Referral Reward"),
            ("daily_reward", "Daily Reward"),
            ("scratch_rewards", "Scratch Rewards (comma)")
        ]
        for key, label in settings:
            markup.add(InlineKeyboardButton(f"✏️ {label}", callback_data=f"editset_{key}"))
        markup.add(InlineKeyboardButton("🔁 Toggle Captcha", callback_data="toggle_captcha"))
        markup.add(InlineKeyboardButton("🔁 Toggle Daily", callback_data="toggle_daily"))
        markup.add(InlineKeyboardButton("🔁 Toggle Scratch", callback_data="toggle_scratch"))
        markup.add(InlineKeyboardButton("🔁 Toggle Shop", callback_data="toggle_shop"))
        markup.add(InlineKeyboardButton("🔙 Back", callback_data="admin_panel"))
        bot.edit_message_text("⚙️ *Bot Settings*", call.message.chat.id, call.message.message_id, reply_markup=markup)
        bot.answer_callback_query(call.id)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error: {str(e)}", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith("editset_"))
@admin_only_callback
def edit_setting_start(call):
    try:
        key = call.data.split("_")[1]
        user_id = call.from_user.id
        temp_data[user_id] = {'edit_key': key}
        current = get_setting(key)
        msg = bot.send_message(call.message.chat.id, f"Current value: `{escape_markdown(current)}`\n\nSend new value:")
        bot.register_next_step_handler(msg, edit_setting_finish)
        bot.answer_callback_query(call.id)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error: {str(e)}", show_alert=True)

def edit_setting_finish(message):
    user_id = message.from_user.id
    if user_id not in temp_data or 'edit_key' not in temp_data[user_id]:
        bot.reply_to(message, "Session expired.")
        return
    key = temp_data[user_id]['edit_key']
    new_val = message.text.strip()
    update_setting(key, new_val)
    del temp_data[user_id]
    bot.reply_to(message, f"✅ Setting `{key}` updated!")

@bot.callback_query_handler(func=lambda call: call.data == "toggle_captcha")
@admin_only_callback
def toggle_captcha(call):
    try:
        current = get_setting("captcha_enabled")
        new = "0" if current == "1" else "1"
        update_setting("captcha_enabled", new)
        bot.answer_callback_query(call.id, f"Captcha {'disabled' if new=='0' else 'enabled'}.")
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error: {str(e)}", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data == "toggle_daily")
@admin_only_callback
def toggle_daily(call):
    try:
        current = get_setting("daily_enabled")
        new = "0" if current == "1" else "1"
        update_setting("daily_enabled", new)
        bot.answer_callback_query(call.id, f"Daily bonus {'disabled' if new=='0' else 'enabled'}.")
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error: {str(e)}", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data == "toggle_scratch")
@admin_only_callback
def toggle_scratch(call):
    try:
        current = get_setting("scratch_enabled")
        new = "0" if current == "1" else "1"
        update_setting("scratch_enabled", new)
        bot.answer_callback_query(call.id, f"Scratch card {'disabled' if new=='0' else 'enabled'}.")
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error: {str(e)}", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data == "toggle_shop")
@admin_only_callback
def toggle_shop(call):
    try:
        current = get_setting("shop_enabled")
        new = "0" if current == "1" else "1"
        update_setting("shop_enabled", new)
        bot.answer_callback_query(call.id, f"Shop {'disabled' if new=='0' else 'enabled'}.")
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error: {str(e)}", show_alert=True)

# --- Promos ---
@bot.callback_query_handler(func=lambda call: call.data == "admin_promos")
@admin_only_callback
def admin_promos(call):
    try:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("➕ Create Promo", callback_data="create_promo"))
        markup.add(InlineKeyboardButton("📋 List Promos", callback_data="list_promos"))
        markup.add(InlineKeyboardButton("🔙 Back", callback_data="admin_panel"))
        bot.edit_message_text("🎁 *Promo Management*", call.message.chat.id, call.message.message_id, reply_markup=markup)
        bot.answer_callback_query(call.id)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error: {str(e)}", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data == "create_promo")
@admin_only_callback
def create_promo_start(call):
    try:
        msg = bot.send_message(call.message.chat.id, "Enter promo code (e.g., SUMMER20):")
        bot.register_next_step_handler(msg, create_promo_code)
        bot.answer_callback_query(call.id)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error: {str(e)}", show_alert=True)

def create_promo_code(message):
    code = message.text.strip().upper()
    user_id = message.from_user.id
    temp_data[user_id] = {'promo_code': code}
    msg = bot.reply_to(message, "Enter reward amount:")
    bot.register_next_step_handler(msg, create_promo_reward)

def create_promo_reward(message):
    try:
        reward = float(message.text.strip())
    except:
        bot.reply_to(message, "Invalid number.")
        return
    user_id = message.from_user.id
    if user_id not in temp_data:
        bot.reply_to(message, "Session expired.")
        return
    temp_data[user_id]['reward'] = reward
    msg = bot.reply_to(message, "Enter max uses (-1 for unlimited):")
    bot.register_next_step_handler(msg, create_promo_usage)

def create_promo_usage(message):
    try:
        max_usage = int(message.text.strip())
    except:
        bot.reply_to(message, "Invalid integer.")
        return
    user_id = message.from_user.id
    if user_id not in temp_data:
        bot.reply_to(message, "Session expired.")
        return
    temp_data[user_id]['max_usage'] = max_usage
    msg = bot.reply_to(message, "Enter expiry date (YYYY-MM-DD) or leave blank for no expiry:")
    bot.register_next_step_handler(msg, create_promo_expiry)

def create_promo_expiry(message):
    expiry = message.text.strip()
    if expiry and not re.match(r"\d{4}-\d{2}-\d{2}", expiry):
        bot.reply_to(message, "Invalid date format. Use YYYY-MM-DD")
        return
    user_id = message.from_user.id
    if user_id not in temp_data:
        bot.reply_to(message, "Session expired.")
        return
    code = temp_data[user_id]['promo_code']
    reward = temp_data[user_id]['reward']
    max_usage = temp_data[user_id]['max_usage']
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("INSERT INTO promos (code, reward, max_usage, expiry_date) VALUES (?, ?, ?, ?)",
                  (code, reward, max_usage, expiry if expiry else None))
        conn.commit()
        bot.reply_to(message, f"✅ Promo {code} created.")
    except sqlite3.IntegrityError:
        bot.reply_to(message, "❌ Promo code already exists.")
    conn.close()
    del temp_data[user_id]

@bot.callback_query_handler(func=lambda call: call.data == "list_promos")
@admin_only_callback
def list_promos(call):
    try:
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM promos")
        promos = c.fetchall()
        conn.close()
        if not promos:
            bot.answer_callback_query(call.id, "No promos.")
            return
        text = "🎟️ *Promo Codes*\n\n"
        for p in promos:
            expiry = escape_markdown(p['expiry_date']) if p['expiry_date'] else 'None'
            text += f"Code: `{p['code']}` | Reward: {p['reward']} | Used: {p['used_count']}/{p['max_usage'] if p['max_usage']!=-1 else '∞'} | Expiry: {expiry}\n"
        bot.send_message(call.message.chat.id, text)
        bot.answer_callback_query(call.id)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error: {str(e)}", show_alert=True)

# --- Tasks Management ---
@bot.callback_query_handler(func=lambda call: call.data == "admin_tasks")
@admin_only_callback
def admin_tasks(call):
    try:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("➕ Add Task", callback_data="add_task"))
        markup.add(InlineKeyboardButton("📋 List Tasks", callback_data="list_tasks_admin"))
        markup.add(InlineKeyboardButton("🔙 Back", callback_data="admin_panel"))
        bot.edit_message_text("📋 *Task Management*", call.message.chat.id, call.message.message_id, reply_markup=markup)
        bot.answer_callback_query(call.id)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error: {str(e)}", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data == "add_task")
@admin_only_callback
def add_task_start(call):
    try:
        msg = bot.send_message(call.message.chat.id, "Enter task description:")
        bot.register_next_step_handler(msg, add_task_desc)
        bot.answer_callback_query(call.id)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error: {str(e)}", show_alert=True)

def add_task_desc(message):
    desc = message.text.strip()
    user_id = message.from_user.id
    temp_data[user_id] = {'task_desc': desc}
    msg = bot.reply_to(message, "Enter task link (or 'none'):")
    bot.register_next_step_handler(msg, add_task_link)

def add_task_link(message):
    link = message.text.strip()
    if link.lower() == "none":
        link = ""
    user_id = message.from_user.id
    if user_id not in temp_data:
        bot.reply_to(message, "Session expired.")
        return
    temp_data[user_id]['link'] = link
    msg = bot.reply_to(message, "Enter reward amount:")
    bot.register_next_step_handler(msg, add_task_reward)

def add_task_reward(message):
    try:
        reward = float(message.text.strip())
    except:
        bot.reply_to(message, "Invalid number.")
        return
    user_id = message.from_user.id
    if user_id not in temp_data:
        bot.reply_to(message, "Session expired.")
        return
    desc = temp_data[user_id]['task_desc']
    link = temp_data[user_id]['link']
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO tasks (description, link, reward) VALUES (?, ?, ?)", (desc, link, reward))
    conn.commit()
    conn.close()
    del temp_data[user_id]
    bot.reply_to(message, "✅ Task added.")

@bot.callback_query_handler(func=lambda call: call.data == "list_tasks_admin")
@admin_only_callback
def list_tasks_admin(call):
    try:
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM tasks")
        tasks = c.fetchall()
        conn.close()
        if not tasks:
            bot.answer_callback_query(call.id, "No tasks.")
            return
        text = "📋 *All Tasks*\n\n"
        markup = InlineKeyboardMarkup()
        for t in tasks:
            desc = escape_markdown(t['description'])
            text += f"ID {t['id']}: {desc} | Reward: {t['reward']}\n"
            markup.add(InlineKeyboardButton(f"❌ Delete task {t['id']}", callback_data=f"deltask_{t['id']}"))
        bot.send_message(call.message.chat.id, text, reply_markup=markup)
        bot.answer_callback_query(call.id)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error: {str(e)}", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith("deltask_"))
@admin_only_callback
def delete_task(call):
    try:
        task_id = int(call.data.split("_")[1])
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()
        conn.close()
        bot.answer_callback_query(call.id, "Task deleted.")
        list_tasks_admin(call)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error: {str(e)}", show_alert=True)

# --- Leaderboard ---
@bot.callback_query_handler(func=lambda call: call.data == "admin_leaderboard")
@admin_only_callback
def admin_leaderboard(call):
    try:
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT user_id, username, balance FROM users ORDER BY balance DESC LIMIT 10")
        top = c.fetchall()
        conn.close()
        if not top:
            bot.send_message(call.message.chat.id, "No users yet.")
            bot.answer_callback_query(call.id)
            return
        text = "🏆 *Top 10 Users by Points*\n\n"
        for i, u in enumerate(top, 1):
            username = escape_markdown(u['username']) if u['username'] else str(u['user_id'])
            text += f"{i}. {username} – {u['balance']} {get_currency()}\n"
        bot.send_message(call.message.chat.id, text)
        bot.answer_callback_query(call.id)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error: {str(e)}", show_alert=True)

# --- Backup ---
@bot.callback_query_handler(func=lambda call: call.data == "admin_backup")
@admin_only_callback
def admin_backup(call):
    try:
        with open(DB_NAME, 'rb') as f:
            bot.send_document(call.message.chat.id, f, caption=f"📅 Database backup {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        bot.answer_callback_query(call.id)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error: {str(e)}", show_alert=True)

# --- Fallback to main menu for any text ---
@bot.message_handler(func=lambda m: True)
def fallback(message):
    is_admin = str(message.from_user.id) == str(ADMIN_ID)
    bot.send_message(message.chat.id, "Use the menu below:", reply_markup=main_menu_kb(is_admin))

# ---------- Main ----------
if __name__ == "__main__":
    init_db()
    threading.Thread(target=run_flask, daemon=True).start()
    logging.basicConfig(level=logging.INFO)
    print("🤖 Bot is polling...")
    time.sleep(5)  # Wait for old instance to release (fixes 409 Conflict)
    bot.infinity_polling()
