import os
import sqlite3
import random
import threading
from datetime import datetime, date
from functools import wraps

from flask import Flask, jsonify
import telebot
from telebot import types

# ---------- কনফিগারেশন ----------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
PORT = int(os.getenv("PORT", 10000))
DB_NAME = "shop_bot.db"

bot = telebot.TeleBot(TOKEN)

# ---------- Flask (হেলথ চেক) ----------
app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({"status": "running", "time": str(datetime.now())})

@app.route('/health')
def health():
    return jsonify({"status": "healthy"}), 200

def run_flask():
    app.run(host='0.0.0.0', port=PORT)

# ---------- ডাটাবেস ----------
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    # Settings
    c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    defaults = {
        "welcome": "🌟 Welcome {name}!",
        "currency": "💎",
        "support": "t.me/admin",
        "rules": "📜 Be nice",
        "channel": "",
        "captcha": "1",
        "shop": "1",
        "referral": "5",
        "daily_on": "1",
        "daily_amt": "10",
        "scratch_on": "1",
        "scratch_rew": "5,10,15,20,25",
    }
    for k, v in defaults.items():
        c.execute("INSERT OR IGNORE INTO settings (key,value) VALUES (?,?)", (k, v))

    # Users
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
        balance REAL DEFAULT 0, total_spent REAL DEFAULT 0,
        referrer INTEGER, joined TEXT DEFAULT CURRENT_TIMESTAMP,
        banned INTEGER DEFAULT 0)""")

    # Categories
    c.execute("CREATE TABLE IF NOT EXISTS categories (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE)")
    
    # Products
    c.execute("""CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT, cat_id INTEGER, type TEXT, name TEXT,
        desc TEXT, price REAL, content TEXT, stock INTEGER DEFAULT -1)""")
    
    # Orders
    c.execute("""CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, product_id INTEGER,
        status TEXT, data TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP)""")
    
    # Tasks
    c.execute("""CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, desc TEXT, link TEXT, reward REAL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS completed (
        user_id INTEGER, task_id INTEGER, PRIMARY KEY (user_id,task_id))""")
    
    # Promos
    c.execute("""CREATE TABLE IF NOT EXISTS promos (
        code TEXT PRIMARY KEY, reward REAL, max_use INTEGER,
        used INTEGER DEFAULT 0, expiry TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS promo_used (
        user_id INTEGER, code TEXT, PRIMARY KEY (user_id,code))""")

    # Transactions
    c.execute("""CREATE TABLE IF NOT EXISTS tx (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount REAL,
        type TEXT, desc TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP)""")

    # Daily / Scratch tracking
    c.execute("CREATE TABLE IF NOT EXISTS daily (user_id INTEGER PRIMARY KEY, last TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS scratch (user_id INTEGER PRIMARY KEY, last TEXT)")

    conn.commit()
    conn.close()

# ---------- ডাটাবেস হেল্পার ----------
def get_setting(key):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key=?", (key,))
    r = c.fetchone()
    conn.close()
    return r[0] if r else None

def set_setting(key, val):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("REPLACE INTO settings VALUES (?,?)", (key, str(val)))
    conn.commit()
    conn.close()

def get_user(user_id):
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    r = c.fetchone()
    conn.close()
    return dict(r) if r else None

def add_user(uid, uname, fname, ref=None):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE user_id=?", (uid,))
    if c.fetchone():
        conn.close()
        return
    c.execute("INSERT INTO users (user_id,username,full_name,referrer) VALUES (?,?,?,?)",
              (uid, uname, fname, ref))
    if ref:
        rew = float(get_setting("referral"))
        c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (rew, ref))
        c.execute("INSERT INTO tx (user_id,amount,type,desc) VALUES (?,?,'referral','New user')", (ref, rew))
    conn.commit()
    conn.close()

# ---------- ইউটিলিটি ----------
def is_admin(user_id):
    return user_id == ADMIN_ID

def main_menu_kb(uid):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    btns = ["🛍 Shop", "👤 Profile", "🎁 Daily", "🎲 Scratch", "📋 Tasks", "ℹ️ Support", "📜 Rules"]
    if is_admin(uid):
        btns.append("⚙️ Admin")
    kb.add(*btns)
    return kb

def gen_captcha():
    emojis = ["😀","😂","😍","🥺","😎","🎉","🔥","⭐","🐶","🐱","🐼"]
    s = random.sample(emojis, 4)
    i = random.randint(0,3)
    ans = s[i]
    s[i] = "___"
    return " ".join(s), ans

# ---------- /start ----------
@bot.message_handler(commands=['start'])
def start(message):
    uid = message.from_user.id
    un = message.from_user.username or ""
    fn = message.from_user.full_name
    args = message.text.split()
    ref = None
    if len(args) > 1 and args[1].isdigit():
        ref_id = int(args[1])
        if ref_id != uid and get_user(ref_id):
            ref = ref_id
    add_user(uid, un, fn, ref)
    
    if get_setting("captcha") == "1":
        q, a = gen_captcha()
        bot.send_message(uid, f"🔒 Captcha:\n{q}\nType missing emoji:", parse_mode='Markdown')
        bot.register_next_step_handler_by_chat_id(uid, captcha_handler, a)
    else:
        welcome(uid)

def captcha_handler(message, ans):
    uid = message.chat.id
    if message.text.strip() == ans:
        welcome(uid)
    else:
        q, a = gen_captcha()
        bot.send_message(uid, f"❌ Wrong. Try:\n{q}")
        bot.register_next_step_handler_by_chat_id(uid, captcha_handler, a)

def welcome(uid):
    w = get_setting("welcome").replace("{name}", bot.get_chat(uid).first_name)
    user = get_user(uid)
    if user and user['banned']:
        bot.send_message(uid, "🚫 You are banned.")
        return
    bot.send_message(uid, w, reply_markup=main_menu_kb(uid), parse_mode='Markdown')

# ---------- টেক্সট হ্যান্ডলার (মেনু) ----------
@bot.message_handler(func=lambda m: m.text == "👤 Profile")
def profile(m):
    uid = m.chat.id
    u = get_user(uid)
    if not u:
        bot.send_message(uid, "Use /start")
        return
    s = u['total_spent']
    if s < 100:
        lvl = "🥉 Bronze"
    elif s < 500:
        lvl = "🥈 Silver"
    else:
        lvl = "🥇 Gold"
    txt = f"""👤 *Profile*
ID: `{u['user_id']}`
💰 Balance: {u['balance']}{get_setting('currency')}
📊 Spent: {s}
🏅 Level: {lvl}
📅 Joined: {u['joined_at'][:10]}"""
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("📦 Orders", callback_data="myord"),
        types.InlineKeyboardButton("📜 History", callback_data="myhist")
    )
    kb.row(types.InlineKeyboardButton("🎁 Redeem", callback_data="promo"))
    bot.send_message(uid, txt, reply_markup=kb, parse_mode='Markdown')
    bot_info = bot.get_me()
    bot.send_message(uid, f"🔗 *Referral link:*\nhttps://t.me/{bot_info.username}?start={uid}", parse_mode='Markdown')

@bot.message_handler(func=lambda m: m.text == "🎁 Daily")
def daily(m):
    uid = m.chat.id
    if get_setting("daily_on") != "1":
        bot.send_message(uid, "❌ Daily bonus disabled")
        return
    today = date.today().isoformat()
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT last FROM daily WHERE user_id=?", (uid,))
    r = c.fetchone()
    if r and r[0] == today:
        conn.close()
        bot.send_message(uid, "⏳ Already claimed today")
        return
    amt = float(get_setting("daily_amt"))
    c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amt, uid))
    c.execute("REPLACE INTO daily VALUES (?,?)", (uid, today))
    c.execute("INSERT INTO tx (user_id,amount,type,desc) VALUES (?,?,'daily','Daily bonus')", (uid, amt))
    conn.commit()
    conn.close()
    bot.send_message(uid, f"🎉 +{amt} Credits")

@bot.message_handler(func=lambda m: m.text == "🎲 Scratch")
def scratch(m):
    uid = m.chat.id
    if get_setting("scratch_on") != "1":
        bot.send_message(uid, "❌ Scratch card disabled")
        return
    today = date.today().isoformat()
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT last FROM scratch WHERE user_id=?", (uid,))
    r = c.fetchone()
    if r and r[0] == today:
        conn.close()
        bot.send_message(uid, "⏳ Already scratched today")
        return
    rewards = [float(x) for x in get_setting("scratch_rew").split(",") if x]
    amt = random.choice(rewards) if rewards else 10
    c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amt, uid))
    c.execute("REPLACE INTO scratch VALUES (?,?)", (uid, today))
    c.execute("INSERT INTO tx (user_id,amount,type,desc) VALUES (?,?,'scratch','Scratch card')", (uid, amt))
    conn.commit()
    conn.close()
    bot.send_message(uid, f"🎲 You won {amt} Credits!")

@bot.message_handler(func=lambda m: m.text == "📋 Tasks")
def tasks(m):
    uid = m.chat.id
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM tasks WHERE id NOT IN (SELECT task_id FROM completed WHERE user_id=?)", (uid,))
    ts = c.fetchall()
    conn.close()
    if not ts:
        bot.send_message(uid, "✅ No tasks available")
        return
    txt = "📋 *Available tasks:*\n"
    kb = types.InlineKeyboardMarkup()
    for t in ts:
        txt += f"\n🔹 {t['desc']} – {t['reward']} Credits"
        kb.add(types.InlineKeyboardButton(f"✅ Complete #{t['id']}", callback_data=f"dotask_{t['id']}"))
    bot.send_message(uid, txt, reply_markup=kb, parse_mode='Markdown')

@bot.message_handler(func=lambda m: m.text == "ℹ️ Support")
def support(m):
    bot.send_message(m.chat.id, f"ℹ️ Support: {get_setting('support')}")

@bot.message_handler(func=lambda m: m.text == "📜 Rules")
def rules(m):
    bot.send_message(m.chat.id, get_setting("rules"), parse_mode='Markdown')

@bot.message_handler(func=lambda m: m.text == "🛍 Shop")
def shop(m):
    uid = m.chat.id
    if get_setting("shop") != "1":
        bot.send_message(uid, "⚠️ Shop is disabled")
        return
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM categories")
    cats = c.fetchall()
    conn.close()
    if not cats:
        bot.send_message(uid, "📭 No categories")
        return
    kb = types.InlineKeyboardMarkup()
    for cat in cats:
        kb.add(types.InlineKeyboardButton(cat['name'], callback_data=f"cat_{cat['id']}"))
    bot.send_message(uid, "📂 *Categories:*", reply_markup=kb, parse_mode='Markdown')

@bot.message_handler(func=lambda m: m.text == "⚙️ Admin")
def admin_panel(m):
    if not is_admin(m.chat.id):
        return
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("📊 Stats", callback_data="astat"),
        types.InlineKeyboardButton("👥 Users", callback_data="auser")
    )
    kb.row(
        types.InlineKeyboardButton("📢 Broadcast", callback_data="abcast"),
        types.InlineKeyboardButton("🛍 Shop", callback_data="ashop")
    )
    kb.row(
        types.InlineKeyboardButton("⚙️ Settings", callback_data="aset"),
        types.InlineKeyboardButton("🎁 Promos", callback_data="apromo")
    )
    kb.row(
        types.InlineKeyboardButton("📋 Tasks", callback_data="atask"),
        types.InlineKeyboardButton("📦 Orders", callback_data="aorder")
    )
    kb.row(types.InlineKeyboardButton("📦 Backup DB", callback_data="abackup"))
    bot.send_message(m.chat.id, "🔧 *Admin Panel*", reply_markup=kb, parse_mode='Markdown')

# ---------- কলব্যাক হ্যান্ডলার ----------
@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    data = call.data
    uid = call.from_user.id
    cid = call.message.chat.id
    mid = call.message.message_id

    # ----- ইউজার কলব্যাক -----
    if data == "myhist":
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM tx WHERE user_id=? ORDER BY created DESC LIMIT 10", (uid,))
        txs = c.fetchall()
        conn.close()
        if not txs:
            bot.edit_message_text("📭 No history", cid, mid)
            return
        txt = "📊 *History:*\n"
        for t in txs:
            txt += f"\n• {t['created'][:10]} {t['type']}: {t['amount']:+} Credits"
        bot.edit_message_text(txt, cid, mid, parse_mode='Markdown')
        return

    if data == "myord":
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("""SELECT o.*, p.name FROM orders o
                     JOIN products p ON o.product_id=p.id
                     WHERE user_id=? ORDER BY created DESC LIMIT 5""", (uid,))
        orders = c.fetchall()
        conn.close()
        if not orders:
            bot.edit_message_text("📭 No orders", cid, mid)
            return
        txt = "📦 *Recent orders:*\n"
        for o in orders:
            txt += f"\n🔹 {o['name']} – {o['status'].upper()}"
        bot.edit_message_text(txt, cid, mid, parse_mode='Markdown')
        return

    if data == "promo":
        msg = bot.send_message(cid, "🎁 Enter promo code:")
        bot.register_next_step_handler_by_chat_id(cid, promo_redeem)
        return

    if data.startswith("dotask_"):
        tid = int(data.split("_")[1])
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM tasks WHERE id=?", (tid,))
        task = c.fetchone()
        if not task:
            conn.close()
            bot.answer_callback_query(call.id, "Task not found")
            return
        c.execute("SELECT * FROM completed WHERE user_id=? AND task_id=?", (uid, tid))
        if c.fetchone():
            conn.close()
            bot.answer_callback_query(call.id, "Already done")
            return
        c.execute("INSERT INTO completed VALUES (?,?)", (uid, tid))
        c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (task['reward'], uid))
        c.execute("INSERT INTO tx (user_id,amount,type,desc) VALUES (?,?,'task',?)", (uid, task['reward'], task['desc']))
        conn.commit()
        conn.close()
        bot.edit_message_text(f"✅ Task done! +{task['reward']} Credits", cid, mid)
        return

    if data.startswith("cat_"):
        cat_id = int(data.split("_")[1])
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM products WHERE cat_id=?", (cat_id,))
        prods = c.fetchall()
        conn.close()
        if not prods:
            bot.edit_message_text("📭 No products", cid, mid)
            return
        kb = types.InlineKeyboardMarkup()
        for p in prods:
            st = "∞" if p['stock'] == -1 else p['stock']
            kb.add(types.InlineKeyboardButton(f"{p['name']} | {p['price']} | Stock:{st}", callback_data=f"prod_{p['id']}"))
        bot.edit_message_text("📦 *Products:*", cid, mid, reply_markup=kb, parse_mode='Markdown')
        return

    if data.startswith("prod_"):
        pid = int(data.split("_")[1])
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM products WHERE id=?", (pid,))
        p = c.fetchone()
        conn.close()
        if not p:
            bot.edit_message_text("Product not found", cid, mid)
            return
        txt = f"""📦 *{p['name']}*
{p['desc']}
💰 Price: {p['price']}{get_setting('currency')}
📦 Stock: {'Unlimited' if p['stock']==-1 else p['stock']}
Type: {p['type']}"""
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton("💳 Buy", callback_data=f"buy_{p['id']}"),
            types.InlineKeyboardButton("🔙 Back", callback_data=f"back_{p['cat_id']}")
        )
        bot.edit_message_text(txt, cid, mid, reply_markup=kb, parse_mode='Markdown')
        return

    if data.startswith("back_"):
        cat_id = int(data.split("_")[1])
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM products WHERE cat_id=?", (cat_id,))
        prods = c.fetchall()
        conn.close()
        kb = types.InlineKeyboardMarkup()
        for p in prods:
            st = "∞" if p['stock'] == -1 else p['stock']
            kb.add(types.InlineKeyboardButton(f"{p['name']} | {p['price']} | Stock:{st}", callback_data=f"prod_{p['id']}"))
        bot.edit_message_text("📦 *Products:*", cid, mid, reply_markup=kb, parse_mode='Markdown')
        return

    if data.startswith("buy_"):
        pid = int(data.split("_")[1])
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM products WHERE id=?", (pid,))
        p = c.fetchone()
        c.execute("SELECT * FROM users WHERE user_id=?", (uid,))
        u = c.fetchone()
        conn.close()
        if not p or not u:
            bot.answer_callback_query(call.id, "Error")
            return
        if p['stock'] != -1 and p['stock'] <= 0:
            bot.answer_callback_query(call.id, "Out of stock")
            return
        if u['balance'] < p['price']:
            bot.answer_callback_query(call.id, "Insufficient balance")
            return
        if p['type'] == 'physical':
            # ask address
            bot.edit_message_text("📦 Please enter your shipping address:", cid, mid)
            bot.register_next_step_handler_by_chat_id(cid, process_physical, pid)
        else:
            # process digital/file immediately
            process_purchase(uid, pid, cid, mid)

    # ----- অ্যাডমিন কলব্যাক -----
    if data == "astat":
        if not is_admin(uid): return
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM users")
        uc = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM orders")
        oc = c.fetchone()[0]
        c.execute("SELECT SUM(price) FROM products JOIN orders ON products.id=orders.product_id")
        rev = c.fetchone()[0] or 0
        conn.close()
        txt = f"📊 *Stats*\n👥 Users: {uc}\n📦 Orders: {oc}\n💰 Revenue: {rev}"
        bot.edit_message_text(txt, cid, mid, parse_mode='Markdown', reply_markup=admin_panel_kb())
        return

    if data == "abackup":
        if not is_admin(uid): return
        bot.edit_message_text("📦 Backup feature coming soon...", cid, mid)
        return

    if data == "abcast":
        if not is_admin(uid): return
        bot.send_message(cid, "📝 Send message to broadcast:")
        bot.register_next_step_handler_by_chat_id(cid, broadcast_message)
        bot.delete_message(cid, mid)
        return

    if data == "ashop":
        if not is_admin(uid): return
        kb = types.InlineKeyboardMarkup()
        kb.row(types.InlineKeyboardButton("➕ Add Category", callback_data="acat"))
        kb.row(types.InlineKeyboardButton("📋 List Categories", callback_data="lcat"))
        kb.row(types.InlineKeyboardButton("🔙 Back", callback_data="apanel"))
        bot.edit_message_text("🛍 *Shop Management*", cid, mid, reply_markup=kb, parse_mode='Markdown')
        return

    if data == "auser":
        if not is_admin(uid): return
        kb = types.InlineKeyboardMarkup()
        kb.row(types.InlineKeyboardButton("🔎 Search User", callback_data="usearch"))
        kb.row(types.InlineKeyboardButton("📋 List Users", callback_data="ulist"))
        bot.edit_message_text("👥 *User Management*", cid, mid, reply_markup=kb, parse_mode='Markdown')
        return

    if data == "aset":
        if not is_admin(uid): return
        kb = types.InlineKeyboardMarkup(row_width=2)
        settings_list = [
            ("Welcome", "set_welcome"),
            ("Currency", "set_currency"),
            ("Support", "set_support"),
            ("Rules", "set_rules"),
            ("Channel", "set_channel"),
            ("Captcha", "set_captcha"),
            ("Shop", "set_shop"),
            ("Referral", "set_referral"),
            ("Daily Amt", "set_daily_amt"),
            ("Daily On", "set_daily_on"),
            ("Scratch Rew", "set_scratch_rew"),
            ("Scratch On", "set_scratch_on"),
        ]
        for name, cb in settings_list:
            kb.add(types.InlineKeyboardButton(name, callback_data=cb))
        kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="apanel"))
        bot.edit_message_text("⚙️ *Settings*", cid, mid, reply_markup=kb, parse_mode='Markdown')
        return

    if data == "apromo":
        if not is_admin(uid): return
        kb = types.InlineKeyboardMarkup()
        kb.row(types.InlineKeyboardButton("➕ Create Promo", callback_data="cpromo"))
        kb.row(types.InlineKeyboardButton("📋 List Promos", callback_data="lpromo"))
        bot.edit_message_text("🎁 *Promo Management*", cid, mid, reply_markup=kb, parse_mode='Markdown')
        return

    if data == "atask":
        if not is_admin(uid): return
        kb = types.InlineKeyboardMarkup()
        kb.row(types.InlineKeyboardButton("➕ Create Task", callback_data="ctask"))
        kb.row(types.InlineKeyboardButton("📋 List Tasks", callback_data="ltask"))
        bot.edit_message_text("📋 *Task Management*", cid, mid, reply_markup=kb, parse_mode='Markdown')
        return

    if data == "aorder":
        if not is_admin(uid): return
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("""SELECT o.*, p.name FROM orders o
                     JOIN products p ON o.product_id=p.id
                     WHERE status='pending'""")
        orders = c.fetchall()
        conn.close()
        if not orders:
            bot.edit_message_text("📭 No pending orders", cid, mid)
            return
        for o in orders:
            txt = f"📦 Order #{o['id']}\nProduct: {o['name']}\nUser: {o['user_id']}\nData: {o['data']}"
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("✅ Deliver", callback_data=f"deliver_{o['id']}"))
            bot.send_message(uid, txt, reply_markup=kb)
        bot.delete_message(cid, mid)
        return

    if data.startswith("deliver_"):
        if not is_admin(uid): return
        oid = int(data.split("_")[1])
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("UPDATE orders SET status='delivered' WHERE id=?", (oid,))
        conn.commit()
        conn.close()
        bot.edit_message_text(f"✅ Order #{oid} delivered", cid, mid)
        return

    if data == "apanel":
        if not is_admin(uid): return
        bot.delete_message(cid, mid)
        admin_panel(call.message)
        return

    if data == "acat":
        if not is_admin(uid): return
        msg = bot.send_message(cid, "📝 Enter category name:")
        bot.register_next_step_handler_by_chat_id(cid, add_category)
        bot.delete_message(cid, mid)
        return

    if data == "lcat":
        if not is_admin(uid): return
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM categories")
        cats = c.fetchall()
        conn.close()
        if not cats:
            bot.edit_message_text("📭 No categories", cid, mid)
            return
        kb = types.InlineKeyboardMarkup()
        for cat in cats:
            kb.row(
                types.InlineKeyboardButton(f"📁 {cat['name']}", callback_data=f"catadm_{cat['id']}"),
                types.InlineKeyboardButton("✏️", callback_data=f"ecat_{cat['id']}"),
                types.InlineKeyboardButton("🗑️", callback_data=f"dcat_{cat['id']}")
            )
        kb.row(types.InlineKeyboardButton("🔙 Back", callback_data="ashop"))
        bot.edit_message_text("📂 *Categories:*", cid, mid, reply_markup=kb, parse_mode='Markdown')
        return

    if data.startswith("catadm_"):
        if not is_admin(uid): return
        cat_id = int(data.split("_")[1])
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM products WHERE cat_id=?", (cat_id,))
        prods = c.fetchall()
        conn.close()
        kb = types.InlineKeyboardMarkup()
        for p in prods:
            kb.row(
                types.InlineKeyboardButton(f"{p['name']} | {p['price']}", callback_data=f"prod_{p['id']}"),
                types.InlineKeyboardButton("✏️", callback_data=f"eprod_{p['id']}"),
                types.InlineKeyboardButton("🗑️", callback_data=f"dprod_{p['id']}")
            )
        kb.row(types.InlineKeyboardButton("➕ Add Product", callback_data=f"aprod_{cat_id}"))
        kb.row(types.InlineKeyboardButton("🔙 Back", callback_data="lcat"))
        bot.edit_message_text("📦 *Products:*", cid, mid, reply_markup=kb, parse_mode='Markdown')
        return

    if data.startswith("ecat_"):
        if not is_admin(uid): return
        cat_id = int(data.split("_")[1])
        msg = bot.send_message(cid, "✏️ Enter new name:")
        bot.register_next_step_handler_by_chat_id(cid, edit_category, cat_id)
        bot.delete_message(cid, mid)
        return

    if data.startswith("dcat_"):
        if not is_admin(uid): return
        cat_id = int(data.split("_")[1])
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("DELETE FROM categories WHERE id=?", (cat_id,))
        conn.commit()
        conn.close()
        bot.edit_message_text("🗑️ Category deleted", cid, mid)
        return

    if data.startswith("aprod_"):
        if not is_admin(uid): return
        cat_id = int(data.split("_")[1])
        bot.send_message(cid, "📝 Enter product name:")
        bot.register_next_step_handler_by_chat_id(cid, add_product_name, cat_id)
        bot.delete_message(cid, mid)
        return

    if data.startswith("eprod_"):
        if not is_admin(uid): return
        prod_id = int(data.split("_")[1])
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton("Name", callback_data=f"en_{prod_id}"),
            types.InlineKeyboardButton("Desc", callback_data=f"ed_{prod_id}")
        )
        kb.row(
            types.InlineKeyboardButton("Price", callback_data=f"ep_{prod_id}"),
            types.InlineKeyboardButton("Stock", callback_data=f"es_{prod_id}")
        )
        kb.row(
            types.InlineKeyboardButton("Content", callback_data=f"ec_{prod_id}"),
            types.InlineKeyboardButton("Type", callback_data=f"et_{prod_id}")
        )
        bot.edit_message_text("✏️ *Edit product:*", cid, mid, reply_markup=kb, parse_mode='Markdown')
        return

    if data.startswith(("en_","ed_","ep_","es_","ec_","et_")):
        if not is_admin(uid): return
        parts = data.split("_")
        field = parts[0]
        prod_id = int(parts[1])
        field_map = {"en":"name","ed":"desc","ep":"price","es":"stock","ec":"content","et":"type"}
        fld = field_map[field]
        msg = bot.send_message(cid, f"✏️ Enter new value for {fld}:")
        bot.register_next_step_handler_by_chat_id(msg.chat.id, update_product_field, prod_id, fld)
        bot.delete_message(cid, mid)
        return

    if data.startswith("dprod_"):
        if not is_admin(uid): return
        prod_id = int(data.split("_")[1])
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("DELETE FROM products WHERE id=?", (prod_id,))
        conn.commit()
        conn.close()
        bot.edit_message_text("🗑️ Product deleted", cid, mid)
        return

    if data == "usearch":
        if not is_admin(uid): return
        msg = bot.send_message(cid, "🆔 Enter user ID or username:")
        bot.register_next_step_handler_by_chat_id(cid, search_user)
        bot.delete_message(cid, mid)
        return

    if data == "ulist":
        if not is_admin(uid): return
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT user_id, full_name, balance FROM users LIMIT 20")
        us = c.fetchall()
        conn.close()
        txt = "👥 *Users (first 20):*\n"
        for u in us:
            txt += f"\n`{u['user_id']}` | {u['full_name']} | {u['balance']}"
        bot.edit_message_text(txt, cid, mid, parse_mode='Markdown')
        return

    if data.startswith("ban_"):
        if not is_admin(uid): return
        target = int(data.split("_")[1])
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("SELECT banned FROM users WHERE user_id=?", (target,))
        row = c.fetchone()
        new = 0 if row[0] else 1
        c.execute("UPDATE users SET banned=? WHERE user_id=?", (new, target))
        conn.commit()
        conn.close()
        bot.answer_callback_query(call.id, f"User {'banned' if new else 'unbanned'}")
        bot.delete_message(cid, mid)
        return

    if data == "cpromo":
        if not is_admin(uid): return
        msg = bot.send_message(cid, "📝 Enter promo code:")
        bot.register_next_step_handler_by_chat_id(cid, add_promo_code)
        bot.delete_message(cid, mid)
        return

    if data == "lpromo":
        if not is_admin(uid): return
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM promos")
        ps = c.fetchall()
        conn.close()
        if not ps:
            bot.edit_message_text("📭 No promos", cid, mid)
            return
        for p in ps:
            txt = f"Code: `{p['code']}`\nReward: {p['reward']}\nUsed: {p['used']}/{p['max_use']}\nExpiry: {p['expiry']}"
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("🗑️ Delete", callback_data=f"dpromo_{p['code']}"))
            bot.send_message(uid, txt, reply_markup=kb, parse_mode='Markdown')
        bot.delete_message(cid, mid)
        return

    if data.startswith("dpromo_"):
        if not is_admin(uid): return
        code = data.split("_")[1]
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("DELETE FROM promos WHERE code=?", (code,))
        conn.commit()
        conn.close()
        bot.edit_message_text("🗑️ Promo deleted", cid, mid)
        return

    if data == "ctask":
        if not is_admin(uid): return
        msg = bot.send_message(cid, "📝 Enter task description:")
        bot.register_next_step_handler_by_chat_id(cid, add_task_desc)
        bot.delete_message(cid, mid)
        return

    if data == "ltask":
        if not is_admin(uid): return
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM tasks")
        ts = c.fetchall()
        conn.close()
        if not ts:
            bot.edit_message_text("📭 No tasks", cid, mid)
            return
        for t in ts:
            txt = f"ID: {t['id']}\nDesc: {t['desc']}\nLink: {t['link']}\nReward: {t['reward']}"
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("🗑️ Delete", callback_data=f"dtask_{t['id']}"))
            bot.send_message(uid, txt, reply_markup=kb)
        bot.delete_message(cid, mid)
        return

    if data.startswith("dtask_"):
        if not is_admin(uid): return
        tid = int(data.split("_")[1])
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("DELETE FROM tasks WHERE id=?", (tid,))
        conn.commit()
        conn.close()
        bot.edit_message_text("🗑️ Task deleted", cid, mid)
        return

    # ----- settings callbacks -----
    if data.startswith("set_"):
        if not is_admin(uid): return
        key = data.replace("set_", "")
        msg = bot.send_message(cid, f"✏️ Enter new value for `{key}`:", parse_mode='Markdown')
        bot.register_next_step_handler_by_chat_id(cid, update_setting, key)
        bot.delete_message(cid, mid)
        return

# ---------- প্রোমো রিডিম ----------
def promo_redeem(message):
    uid = message.chat.id
    code = message.text.strip()
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM promos WHERE code=?", (code,))
    p = c.fetchone()
    if not p:
        conn.close()
        bot.send_message(uid, "❌ Invalid code")
        return
    if p['expiry'] and p['expiry'] < datetime.now().strftime("%Y-%m-%d"):
        conn.close()
        bot.send_message(uid, "❌ Code expired")
        return
    if p['max_use'] != -1 and p['used'] >= p['max_use']:
        conn.close()
        bot.send_message(uid, "❌ Usage limit reached")
        return
    c.execute("SELECT * FROM promo_used WHERE user_id=? AND code=?", (uid, code))
    if c.fetchone():
        conn.close()
        bot.send_message(uid, "❌ Already used")
        return
    c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (p['reward'], uid))
    c.execute("UPDATE promos SET used=used+1 WHERE code=?", (code,))
    c.execute("INSERT INTO promo_used VALUES (?,?)", (uid, code))
    c.execute("INSERT INTO tx (user_id,amount,type,desc) VALUES (?,?,'promo',?)", (uid, p['reward'], code))
    conn.commit()
    conn.close()
    bot.send_message(uid, f"✅ Redeemed! +{p['reward']} Credits")

# ---------- ফিজিক্যাল প্রোডাক্টের জন্য ----------
def process_physical(message, pid):
    uid = message.chat.id
    addr = message.text
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM products WHERE id=?", (pid,))
    p = c.fetchone()
    c.execute("SELECT * FROM users WHERE user_id=?", (uid,))
    u = c.fetchone()
    if not p or not u:
        conn.close()
        bot.send_message(uid, "Error")
        return
    if p['stock'] != -1 and p['stock'] <= 0:
        bot.send_message(uid, "Out of stock")
        return
    if u['balance'] < p['price']:
        bot.send_message(uid, "Insufficient balance")
        return
    new_bal = u['balance'] - p['price']
    new_stock = p['stock']-1 if p['stock']!=-1 else -1
    c.execute("UPDATE users SET balance=?, total_spent=total_spent+? WHERE user_id=?", (new_bal, p['price'], uid))
    if new_stock != -1:
        c.execute("UPDATE products SET stock=? WHERE id=?", (new_stock, pid))
    c.execute("INSERT INTO orders (user_id,product_id,status,data) VALUES (?,?,'pending',?)", (uid, pid, addr))
    c.execute("INSERT INTO tx (user_id,amount,type,desc) VALUES (?,?,'purchase',?)", (uid, -p['price'], p['name']))
    conn.commit()
    conn.close()
    bot.send_message(uid, f"✅ Order placed! Balance: {new_bal} Credits")

# ---------- ডিজিটাল/ফাইল কেনার প্রক্রিয়া ----------
def process_purchase(uid, pid, cid, mid):
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM products WHERE id=?", (pid,))
    p = c.fetchone()
    c.execute("SELECT * FROM users WHERE user_id=?", (uid,))
    u = c.fetchone()
    if not p or not u:
        conn.close()
        bot.send_message(uid, "Error")
        return
    if p['stock'] != -1 and p['stock'] <= 0:
        bot.send_message(uid, "Out of stock")
        return
    if u['balance'] < p['price']:
        bot.send_message(uid, "Insufficient balance")
        return
    new_bal = u['balance'] - p['price']
    new_stock = p['stock']-1 if p['stock']!=-1 else -1
    c.execute("UPDATE users SET balance=?, total_spent=total_spent+? WHERE user_id=?", (new_bal, p['price'], uid))
    if new_stock != -1:
        c.execute("UPDATE products SET stock=? WHERE id=?", (new_stock, pid))
    status = "delivered" if p['type'] in ('digital','file') else "pending"
    data = p['content'] if status == "delivered" else ""
    c.execute("INSERT INTO orders (user_id,product_id,status,data) VALUES (?,?,?,?)", (uid, pid, status, data))
    c.execute("INSERT INTO tx (user_id,amount,type,desc) VALUES (?,?,'purchase',?)", (uid, -p['price'], p['name']))
    conn.commit()
    conn.close()
    bot.send_message(uid, f"✅ Purchase successful! Balance: {new_bal} Credits")
    if p['type'] == 'file':
        try:
            bot.send_document(uid, p['content'], caption=p['name'])
        except:
            bot.send_message(uid, "❌ Failed to deliver file. Contact admin.")
    elif p['type'] == 'digital':
        bot.send_message(uid, f"📦 Your item:\n`{p['content']}`", parse_mode='Markdown')

# ---------- অ্যাডমিন ব্রডকাস্ট ----------
def broadcast_message(message):
    uid = message.chat.id
    if not is_admin(uid):
        return
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT user_id FROM users")
    users = c.fetchall()
    conn.close()
    success = 0
    for (user_id,) in users:
        try:
            bot.copy_message(user_id, uid, message.message_id)
            success += 1
        except:
            pass
    bot.send_message(uid, f"✅ Broadcast sent to {success} users")

# ---------- অ্যাডমিন ক্যাটাগরি ----------
def add_category(message):
    uid = message.chat.id
    if not is_admin(uid):
        return
    name = message.text
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (name,))
    conn.commit()
    conn.close()
    bot.send_message(uid, f"✅ Category '{name}' added")

def edit_category(message, cat_id):
    uid = message.chat.id
    if not is_admin(uid):
        return
    new_name = message.text
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE categories SET name=? WHERE id=?", (new_name, cat_id))
    conn.commit()
    conn.close()
    bot.send_message(uid, "✅ Category updated")

# ---------- অ্যাডমিন প্রোডাক্ট ----------
def add_product_name(message, cat_id):
    uid = message.chat.id
    if not is_admin(uid):
        return
    name = message.text
    bot.send_message(uid, "📝 Enter description:")
    bot.register_next_step_handler_by_chat_id(uid, add_product_desc, cat_id, name)

def add_product_desc(message, cat_id, name):
    uid = message.chat.id
    desc = message.text
    bot.send_message(uid, "💰 Enter price:")
    bot.register_next_step_handler_by_chat_id(uid, add_product_price, cat_id, name, desc)

def add_product_price(message, cat_id, name, desc):
    uid = message.chat.id
    try:
        price = float(message.text)
    except:
        bot.send_message(uid, "❌ Invalid price. Try again:")
        bot.register_next_step_handler_by_chat_id(uid, add_product_price, cat_id, name, desc)
        return
    bot.send_message(uid, "📦 Enter type (digital/file/physical):")
    bot.register_next_step_handler_by_chat_id(uid, add_product_type, cat_id, name, desc, price)

def add_product_type(message, cat_id, name, desc, price):
    uid = message.chat.id
    ptype = message.text.lower()
    if ptype not in ('digital','file','physical'):
        bot.send_message(uid, "❌ Invalid type. Choose digital/file/physical:")
        bot.register_next_step_handler_by_chat_id(uid, add_product_type, cat_id, name, desc, price)
        return
    if ptype == 'physical':
        bot.send_message(uid, "📦 Enter stock (-1 for unlimited):")
        bot.register_next_step_handler_by_chat_id(uid, add_product_stock, cat_id, name, desc, price, ptype, "Physical item")
    else:
        bot.send_message(uid, "📄 Enter content (text or upload file):")
        bot.register_next_step_handler_by_chat_id(uid, add_product_content, cat_id, name, desc, price, ptype)

def add_product_content(message, cat_id, name, desc, price, ptype):
    uid = message.chat.id
    if message.document:
        content = message.document.file_id
    elif message.photo:
        content = message.photo[-1].file_id
    else:
        content = message.text or "No content"
    bot.send_message(uid, "📦 Enter stock (-1 for unlimited):")
    bot.register_next_step_handler_by_chat_id(uid, add_product_stock, cat_id, name, desc, price, ptype, content)

def add_product_stock(message, cat_id, name, desc, price, ptype, content):
    uid = message.chat.id
    try:
        stock = int(message.text)
    except:
        bot.send_message(uid, "❌ Invalid stock. Enter integer:")
        bot.register_next_step_handler_by_chat_id(uid, add_product_stock, cat_id, name, desc, price, ptype, content)
        return
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""INSERT INTO products (cat_id, type, name, desc, price, content, stock)
                 VALUES (?,?,?,?,?,?,?)""", (cat_id, ptype, name, desc, price, content, stock))
    conn.commit()
    conn.close()
    bot.send_message(uid, "✅ Product added!")

def update_product_field(message, prod_id, field):
    uid = message.chat.id
    val = message.text
    if field in ('price','stock'):
        try:
            val = float(val) if field=='price' else int(val)
        except:
            bot.send_message(uid, "❌ Invalid number. Try again:")
            bot.register_next_step_handler_by_chat_id(uid, update_product_field, prod_id, field)
            return
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(f"UPDATE products SET {field}=? WHERE id=?", (val, prod_id))
    conn.commit()
    conn.close()
    bot.send_message(uid, "✅ Product updated")

# ---------- অ্যাডমিন ইউজার সার্চ ----------
def search_user(message):
    uid = message.chat.id
    if not is_admin(uid):
        return
    q = message.text
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if q.isdigit():
        c.execute("SELECT * FROM users WHERE user_id=?", (int(q),))
    else:
        c.execute("SELECT * FROM users WHERE username=?", (q.replace('@',''),))
    u = c.fetchone()
    conn.close()
    if not u:
        bot.send_message(uid, "❌ User not found")
        return
    txt = f"""👤 *User*
ID: `{u['user_id']}`
Name: {u['full_name']}
Username: @{u['username']}
Balance: {u['balance']}
Banned: {u['banned']}"""
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("💰 Give", callback_data=f"give_{u['user_id']}"),
        types.InlineKeyboardButton("🚫 Ban", callback_data=f"ban_{u['user_id']}")
    )
    bot.send_message(uid, txt, reply_markup=kb, parse_mode='Markdown')

@bot.callback_query_handler(func=lambda c: c.data.startswith("give_"))
def give_balance_callback(call):
    uid = call.from_user.id
    if not is_admin(uid):
        return
    target = int(call.data.split("_")[1])
    msg = bot.send_message(uid, "💰 Enter amount (+/-):")
    bot.register_next_step_handler_by_chat_id(uid, update_user_balance, target)
    bot.delete_message(call.message.chat.id, call.message.message_id)

def update_user_balance(message, target):
    uid = message.chat.id
    try:
        amt = float(message.text)
    except:
        bot.send_message(uid, "❌ Invalid amount")
        return
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amt, target))
    conn.commit()
    conn.close()
    bot.send_message(uid, f"✅ Balance updated by {amt}")

# ---------- অ্যাডমিন প্রোমো ----------
def add_promo_code(message):
    uid = message.chat.id
    if not is_admin(uid):
        return
    code = message.text
    bot.send_message(uid, "💰 Enter reward amount:")
    bot.register_next_step_handler_by_chat_id(uid, add_promo_reward, code)

def add_promo_reward(message, code):
    uid = message.chat.id
    try:
        rew = float(message.text)
    except:
        bot.send_message(uid, "❌ Invalid number")
        return
    bot.send_message(uid, "🔢 Enter max uses (-1 for unlimited):")
    bot.register_next_step_handler_by_chat_id(uid, add_promo_limit, code, rew)

def add_promo_limit(message, code, rew):
    uid = message.chat.id
    try:
        lim = int(message.text)
    except:
        bot.send_message(uid, "❌ Invalid number")
        return
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO promos (code, reward, max_use, expiry) VALUES (?,?,?,?)",
              (code, rew, lim, "2099-12-31"))
    conn.commit()
    conn.close()
    bot.send_message(uid, f"✅ Promo {code} created")

# ---------- অ্যাডমিন টাস্ক ----------
def add_task_desc(message):
    uid = message.chat.id
    if not is_admin(uid):
        return
    desc = message.text
    bot.send_message(uid, "🔗 Enter task link (or None):")
    bot.register_next_step_handler_by_chat_id(uid, add_task_link, desc)

def add_task_link(message, desc):
    uid = message.chat.id
    link = message.text
    bot.send_message(uid, "💰 Enter reward:")
    bot.register_next_step_handler_by_chat_id(uid, add_task_reward, desc, link)

def add_task_reward(message, desc, link):
    uid = message.chat.id
    try:
        rew = float(message.text)
    except:
        bot.send_message(uid, "❌ Invalid number")
        return
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO tasks (desc, link, reward) VALUES (?,?,?)", (desc, link, rew))
    conn.commit()
    conn.close()
    bot.send_message(uid, "✅ Task created")

# ---------- অ্যাডমিন সেটিংস ----------
def update_setting(message, key):
    uid = message.chat.id
    val = message.text
    set_setting(key, val)
    bot.send_message(uid, f"✅ {key} updated")

# ---------- অ্যাডমিন কীবোর্ড রিইউজ ----------
def admin_panel_kb():
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("📊 Stats", callback_data="astat"),
        types.InlineKeyboardButton("👥 Users", callback_data="auser")
    )
    kb.row(
        types.InlineKeyboardButton("📢 Broadcast", callback_data="abcast"),
        types.InlineKeyboardButton("🛍 Shop", callback_data="ashop")
    )
    kb.row(
        types.InlineKeyboardButton("⚙️ Settings", callback_data="aset"),
        types.InlineKeyboardButton("🎁 Promos", callback_data="apromo")
    )
    kb.row(
        types.InlineKeyboardButton("📋 Tasks", callback_data="atask"),
        types.InlineKeyboardButton("📦 Orders", callback_data="aorder")
    )
    kb.row(types.InlineKeyboardButton("📦 Backup DB", callback_data="abackup"))
    return kb

# ---------- মূল ফাংশন ----------
def main():
    init_db()
    threading.Thread(target=run_flask, daemon=True).start()
    print(f"🤖 Bot running on port {PORT}")
    bot.infinity_polling()

if __name__ == "__main__":
    main()
