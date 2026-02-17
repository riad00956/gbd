import os
import sqlite3
import random
import json
import threading
from datetime import datetime, date
from functools import wraps

from flask import Flask, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ParseMode
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext, CallbackQueryHandler, ConversationHandler

# ---------- Configuration ----------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
PORT = int(os.getenv("PORT", 10000))
DB_NAME = "shop_bot.db"

# ---------- Flask ----------
app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({"status": "running", "time": str(datetime.now())})

@app.route('/health')
def health():
    return jsonify({"status": "healthy"}), 200

def run_flask():
    app.run(host='0.0.0.0', port=PORT)

# ---------- Database ----------
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
    for k,v in defaults.items():
        c.execute("INSERT OR IGNORE INTO settings (key,value) VALUES (?,?)", (k,v))

    # Users
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
        balance REAL DEFAULT 0, total_spent REAL DEFAULT 0,
        referrer INTEGER, joined TEXT DEFAULT CURRENT_TIMESTAMP,
        banned INTEGER DEFAULT 0)""")

    # Categories
    c.execute("CREATE TABLE IF NOT EXISTS categories (id INTEGER PRIMARY KEY, name TEXT UNIQUE)")
    
    # Products
    c.execute("""CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY, cat_id INTEGER, type TEXT, name TEXT,
        desc TEXT, price REAL, content TEXT, stock INTEGER DEFAULT -1)""")
    
    # Orders
    c.execute("""CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY, user_id INTEGER, product_id INTEGER,
        status TEXT, data TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP)""")
    
    # Tasks
    c.execute("CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, desc TEXT, link TEXT, reward REAL)")
    c.execute("CREATE TABLE IF NOT EXISTS completed (user_id INTEGER, task_id INTEGER, PRIMARY KEY (user_id,task_id))")
    
    # Promos
    c.execute("""CREATE TABLE IF NOT EXISTS promos (
        code TEXT PRIMARY KEY, reward REAL, max_use INTEGER,
        used INTEGER DEFAULT 0, expiry TEXT)""")
    c.execute("CREATE TABLE IF NOT EXISTS promo_used (user_id INTEGER, code TEXT, PRIMARY KEY (user_id,code))")

    # Transactions
    c.execute("""CREATE TABLE IF NOT EXISTS tx (
        id INTEGER PRIMARY KEY, user_id INTEGER, amount REAL,
        type TEXT, desc TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP)""")

    # Daily/Scratch
    c.execute("CREATE TABLE IF NOT EXISTS daily (user_id INTEGER PRIMARY KEY, last TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS scratch (user_id INTEGER PRIMARY KEY, last TEXT)")

    conn.commit()
    conn.close()

# ---------- Helpers ----------
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
    c.execute("REPLACE INTO settings VALUES (?,?)", (key,str(val)))
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

# ---------- States ----------
(CAPTCHA, ADDR, PROMO, CAT_NAME, CAT_EDIT, P_NAME, P_DESC, P_PRICE, P_TYPE, P_CONTENT, P_STOCK,
 SEARCH, BALANCE, SET_VAL, TASK_DESC, TASK_LINK, TASK_REW, PROMO_REW, PROMO_LIM) = range(19)

# ---------- Captcha ----------
EMOJIS = ["😀","😂","😍","🥺","😎","🎉","🔥","⭐","🐶","🐱","🐼"]

def gen_captcha():
    s = random.sample(EMOJIS, 4)
    i = random.randint(0,3)
    a = s[i]
    s[i] = "___"
    return " ".join(s), a

# ---------- Admin Decorator ----------
def admin_only(f):
    @wraps(f)
    def wrapper(up, ctx):
        if up.effective_user.id != ADMIN_ID:
            up.message.reply_text("⛔ No")
            return
        return f(up, ctx)
    return wrapper

# ---------- Keyboards ----------
def main_kb(admin=False):
    kb = [["🛍 Shop","👤 Profile"],["🎁 Daily","🎲 Scratch"],["📋 Tasks","ℹ️ Support"],["📜 Rules"]]
    if admin: kb.append(["⚙️ Admin"])
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)

def admin_kb():
    b = [
        [InlineKeyboardButton("📊 Stats",cb="astat"), InlineKeyboardButton("👥 Users",cb="auser")],
        [InlineKeyboardButton("📢 Broadcast",cb="abcast"), InlineKeyboardButton("🛍 Shop",cb="ashop")],
        [InlineKeyboardButton("⚙️ Settings",cb="aset"), InlineKeyboardButton("🎁 Promos",cb="apromo")],
        [InlineKeyboardButton("📋 Tasks",cb="atask"), InlineKeyboardButton("📦 Orders",cb="aorder")],
    ]
    return InlineKeyboardMarkup(b)

# ---------- Start ----------
def start(up, ctx):
    uid = up.effective_user.id
    un = up.effective_user.username
    fn = up.effective_user.full_name
    args = ctx.args
    ref = int(args[0]) if args and args[0].isdigit() and int(args[0])!=uid and get_user(int(args[0])) else None
    add_user(uid, un, fn, ref)
    
    if get_setting("captcha")=="1":
        q,a = gen_captcha()
        ctx.user_data['cap'] = a
        up.message.reply_text(f"🔒 Captcha:\n{q}\nType missing:", parse_mode=ParseMode.MARKDOWN)
        return CAPTCHA
    welcome(up, ctx)
    return -1

def captcha(up, ctx):
    if up.message.text.strip() == ctx.user_data.get('cap'):
        welcome(up, ctx)
        return -1
    q,a = gen_captcha()
    ctx.user_data['cap'] = a
    up.message.reply_text(f"❌ Try:\n{q}")
    return CAPTCHA

def welcome(up, ctx):
    w = get_setting("welcome").replace("{name}", up.effective_user.full_name)
    u = get_user(up.effective_user.id)
    if u and u['banned']:
        up.message.reply_text("🚫 Banned")
        return
    admin = up.effective_user.id == ADMIN_ID
    up.message.reply_text(w, reply_markup=main_kb(admin), parse_mode=ParseMode.MARKDOWN)

# ---------- Profile ----------
def profile(up, ctx):
    u = get_user(up.effective_user.id)
    if not u:
        up.message.reply_text("Use /start")
        return
    s = u['total_spent']
    l = "🥉" if s<100 else "🥈" if s<500 else "🥇"
    txt = f"👤 Profile\nID: `{u['user_id']}`\n💰 {u['balance']}{get_setting('currency')}\n📊 Spent: {s}\n🏅 {l}\n📅 {u['joined_at'][:10]}"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Orders",cb="myord"), InlineKeyboardButton("📜 History",cb="myhist")],
        [InlineKeyboardButton("🎁 Redeem",cb="promo")]
    ])
    up.message.reply_text(txt, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    bot = ctx.bot.get_me()
    up.message.reply_text(f"🔗 Referral:\nhttps://t.me/{bot.username}?start={u['user_id']}")

def my_hist(up, ctx):
    q = up.callback_query
    q.answer()
    uid = q.from_user.id
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM tx WHERE user_id=? ORDER BY created DESC LIMIT 10", (uid,))
    txs = c.fetchall()
    conn.close()
    if not txs:
        q.edit_message_text("📭 No history")
        return
    txt = "📊 History:\n"
    for t in txs:
        txt += f"\n• {t['created'][:10]} {t['type']}: {t['amount']:+}"
    q.edit_message_text(txt)

# ---------- Daily ----------
def daily(up, ctx):
    if get_setting("daily_on")!="1":
        up.message.reply_text("❌ Daily off")
        return
    uid = up.effective_user.id
    today = date.today().isoformat()
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT last FROM daily WHERE user_id=?", (uid,))
    r = c.fetchone()
    if r and r[0]==today:
        conn.close()
        up.message.reply_text("⏳ Already claimed")
        return
    amt = float(get_setting("daily_amt"))
    c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amt, uid))
    c.execute("REPLACE INTO daily VALUES (?,?)", (uid, today))
    c.execute("INSERT INTO tx (user_id,amount,type,desc) VALUES (?,?,'daily','Daily bonus')", (uid, amt))
    conn.commit()
    conn.close()
    up.message.reply_text(f"🎉 +{amt} Credits")

# ---------- Scratch ----------
def scratch(up, ctx):
    if get_setting("scratch_on")!="1":
        up.message.reply_text("❌ Scratch off")
        return
    uid = up.effective_user.id
    today = date.today().isoformat()
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT last FROM scratch WHERE user_id=?", (uid,))
    r = c.fetchone()
    if r and r[0]==today:
        conn.close()
        up.message.reply_text("⏳ Already scratched")
        return
    rews = [float(x) for x in get_setting("scratch_rew").split(",") if x]
    amt = random.choice(rews) if rews else 10
    c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amt, uid))
    c.execute("REPLACE INTO scratch VALUES (?,?)", (uid, today))
    c.execute("INSERT INTO tx (user_id,amount,type,desc) VALUES (?,?,'scratch','Scratch')", (uid, amt))
    conn.commit()
    conn.close()
    up.message.reply_text(f"🎲 You won {amt} Credits!")

# ---------- Tasks ----------
def tasks(up, ctx):
    uid = up.effective_user.id
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM tasks WHERE id NOT IN (SELECT task_id FROM completed WHERE user_id=?)", (uid,))
    ts = c.fetchall()
    conn.close()
    if not ts:
        up.message.reply_text("✅ No tasks")
        return
    txt = "📋 Tasks:\n"
    kb = []
    for t in ts:
        txt += f"\n🔹 {t['desc']} – {t['reward']}"
        kb.append([InlineKeyboardButton(f"✅ Do #{t['id']}", cb=f"dotask_{t['id']}")])
    up.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

def do_task(up, ctx):
    q = up.callback_query
    q.answer()
    tid = int(q.data.split("_")[1])
    uid = q.from_user.id
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM tasks WHERE id=?", (tid,))
    t = c.fetchone()
    if not t:
        conn.close()
        q.edit_message_text("Task not found")
        return
    c.execute("SELECT * FROM completed WHERE user_id=? AND task_id=?", (uid, tid))
    if c.fetchone():
        conn.close()
        q.edit_message_text("Already done")
        return
    c.execute("INSERT INTO completed VALUES (?,?)", (uid, tid))
    c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (t['reward'], uid))
    c.execute("INSERT INTO tx (user_id,amount,type,desc) VALUES (?,?,'task',?)", (uid, t['reward'], t['desc']))
    conn.commit()
    conn.close()
    q.edit_message_text(f"✅ +{t['reward']} Credits")

# ---------- Promo ----------
def promo_ask(up, ctx):
    q = up.callback_query
    q.answer()
    q.message.reply_text("🎁 Enter code:")
    return PROMO

def promo_use(up, ctx):
    code = up.message.text.strip()
    uid = up.effective_user.id
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM promos WHERE code=?", (code,))
    p = c.fetchone()
    if not p:
        conn.close()
        up.message.reply_text("❌ Invalid")
        return -1
    if p['expiry'] and p['expiry'] < datetime.now().strftime("%Y-%m-%d"):
        conn.close()
        up.message.reply_text("❌ Expired")
        return -1
    if p['max_use']!=-1 and p['used']>=p['max_use']:
        conn.close()
        up.message.reply_text("❌ Used up")
        return -1
    c.execute("SELECT * FROM promo_used WHERE user_id=? AND code=?", (uid, code))
    if c.fetchone():
        conn.close()
        up.message.reply_text("❌ Already used")
        return -1
    c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (p['reward'], uid))
    c.execute("UPDATE promos SET used=used+1 WHERE code=?", (code,))
    c.execute("INSERT INTO promo_used VALUES (?,?)", (uid, code))
    c.execute("INSERT INTO tx (user_id,amount,type,desc) VALUES (?,?,'promo',?)", (uid, p['reward'], code))
    conn.commit()
    conn.close()
    up.message.reply_text(f"✅ +{p['reward']} Credits")
    return -1

# ---------- Shop ----------
def shop(up, ctx):
    if get_setting("shop")!="1":
        up.message.reply_text("⚠️ Shop off")
        return
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM categories")
    cats = c.fetchall()
    conn.close()
    if not cats:
        up.message.reply_text("📭 No categories")
        return
    kb = [[InlineKeyboardButton(c['name'], cb=f"cat_{c['id']}")] for c in cats]
    up.message.reply_text("📂 Categories:", reply_markup=InlineKeyboardMarkup(kb))

def cat_prod(up, ctx):
    q = up.callback_query
    q.answer()
    cid = int(q.data.split("_")[1])
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM products WHERE cat_id=?", (cid,))
    prods = c.fetchall()
    conn.close()
    if not prods:
        q.edit_message_text("📭 No products")
        return
    kb = []
    for p in prods:
        st = "∞" if p['stock']==-1 else p['stock']
        kb.append([InlineKeyboardButton(f"{p['name']} | {p['price']} | Stock:{st}", cb=f"prod_{p['id']}")])
    q.edit_message_text("📦 Products:", reply_markup=InlineKeyboardMarkup(kb))

def prod_detail(up, ctx):
    q = up.callback_query
    q.answer()
    pid = int(q.data.split("_")[1])
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM products WHERE id=?", (pid,))
    p = c.fetchone()
    conn.close()
    if not p:
        q.edit_message_text("Not found")
        return
    txt = f"""📦 **{p['name']}**
{p['desc']}
💰 {p['price']}{get_setting('currency')}
📦 {'Unlimited' if p['stock']==-1 else p['stock']}
Type: {p['type']}"""
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Buy", cb=f"buy_{p['id']}")],
        [InlineKeyboardButton("🔙 Back", cb=f"back_{p['cat_id']}")]
    ])
    q.edit_message_text(txt, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    ctx.user_data['prod'] = dict(p)

def buy(up, ctx):
    q = up.callback_query
    q.answer()
    pid = int(q.data.split("_")[1])
    uid = q.from_user.id
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM products WHERE id=?", (pid,))
    p = c.fetchone()
    c.execute("SELECT * FROM users WHERE user_id=?", (uid,))
    u = c.fetchone()
    conn.close()
    if not p or not u:
        q.edit_message_text("Error")
        return
    if p['stock']!=-1 and p['stock']<=0:
        q.edit_message_text("Out of stock")
        return
    if u['balance'] < p['price']:
        q.edit_message_text("Insufficient balance")
        return
    if p['type'] == 'physical':
        ctx.user_data['buy_pid'] = pid
        q.message.reply_text("📦 Enter address:")
        return ADDR
    # Process
    nb = u['balance'] - p['price']
    ns = p['stock']-1 if p['stock']!=-1 else -1
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE users SET balance=?, total_spent=total_spent+? WHERE user_id=?", (nb, p['price'], uid))
    if ns != -1:
        c.execute("UPDATE products SET stock=? WHERE id=?", (ns, pid))
    status = "delivered" if p['type'] in ('digital','file') else "pending"
    data = p['content'] if status=="delivered" else ""
    c.execute("INSERT INTO orders (user_id,product_id,status,data) VALUES (?,?,?,?)", (uid, pid, status, data))
    c.execute("INSERT INTO tx (user_id,amount,type,desc) VALUES (?,?,'purchase',?)", (uid, -p['price'], p['name']))
    conn.commit()
    conn.close()
    ctx.bot.send_message(uid, f"✅ Done! Balance: {nb}")
    if p['type'] == 'file':
        try: ctx.bot.send_document(uid, p['content'], caption=p['name'])
        except: pass
    elif p['type'] == 'digital':
        ctx.bot.send_message(uid, f"📦 Item:\n`{p['content']}`", parse_mode=ParseMode.MARKDOWN)
    return -1

def addr_save(up, ctx):
    addr = up.message.text
    pid = ctx.user_data['buy_pid']
    uid = up.effective_user.id
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM products WHERE id=?", (pid,))
    p = c.fetchone()
    c.execute("SELECT * FROM users WHERE user_id=?", (uid,))
    u = c.fetchone()
    conn.close()
    nb = u['balance'] - p['price']
    ns = p['stock']-1 if p['stock']!=-1 else -1
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE users SET balance=?, total_spent=total_spent+? WHERE user_id=?", (nb, p['price'], uid))
    if ns != -1:
        c.execute("UPDATE products SET stock=? WHERE id=?", (ns, pid))
    c.execute("INSERT INTO orders (user_id,product_id,status,data) VALUES (?,?,?,?)", (uid, pid, 'pending', addr))
    c.execute("INSERT INTO tx (user_id,amount,type,desc) VALUES (?,?,'purchase',?)", (uid, -p['price'], p['name']))
    conn.commit()
    conn.close()
    up.message.reply_text(f"✅ Ordered! Balance: {nb}")
    return -1

def my_orders(up, ctx):
    q = up.callback_query
    q.answer()
    uid = q.from_user.id
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT o.*, p.name FROM orders o JOIN products p ON o.product_id=p.id WHERE user_id=? ORDER BY created DESC LIMIT 5", (uid,))
    os = c.fetchall()
    conn.close()
    if not os:
        q.edit_message_text("📭 No orders")
        return
    txt = "📦 Orders:\n"
    for o in os:
        txt += f"\n🔹 {o['name']} – {o['status'].upper()}"
    q.edit_message_text(txt)

def back(up, ctx):
    q = up.callback_query
    q.answer()
    cid = int(q.data.split("_")[1])
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM products WHERE cat_id=?", (cid,))
    prods = c.fetchall()
    conn.close()
    kb = []
    for p in prods:
        st = "∞" if p['stock']==-1 else p['stock']
        kb.append([InlineKeyboardButton(f"{p['name']} | {p['price']} | Stock:{st}", cb=f"prod_{p['id']}")])
    q.edit_message_text("📦 Products:", reply_markup=InlineKeyboardMarkup(kb))

# ---------- Support/Rules ----------
def support(up, ctx):
    up.message.reply_text(f"ℹ️ Support: {get_setting('support')}")

def rules(up, ctx):
    up.message.reply_text(get_setting("rules"), parse_mode=ParseMode.MARKDOWN)

# ---------- Admin ----------
@admin_only
def admin(up, ctx):
    up.message.reply_text("🔧 Admin", reply_markup=admin_kb())

def astats(up, ctx):
    q = up.callback_query
    q.answer()
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    uc = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM orders")
    oc = c.fetchone()[0]
    c.execute("SELECT SUM(price) FROM products JOIN orders ON products.id=orders.product_id")
    rev = c.fetchone()[0] or 0
    conn.close()
    txt = f"📊 Stats\n👥 Users: {uc}\n📦 Orders: {oc}\n💰 Revenue: {rev}"
    q.edit_message_text(txt, reply_markup=admin_kb())

def abackup(up, ctx):
    q = up.callback_query
    q.answer()
    q.edit_message_text("📦 Backup coming...")

def abcast(up, ctx):
    q = up.callback_query
    q.answer()
    q.message.reply_text("📝 Send message:")

def ashop(up, ctx):
    q = up.callback_query
    q.answer()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Category", cb="acat")],
        [InlineKeyboardButton("📋 List Categories", cb="lcat")],
        [InlineKeyboardButton("🔙 Back", cb="apanel")]
    ])
    q.edit_message_text("🛍 Shop Mgmt", reply_markup=kb)

def acat(up, ctx):
    q = up.callback_query
    q.answer()
    q.message.reply_text("📝 Category name:")
    return CAT_NAME

def add_cat(up, ctx):
    n = up.message.text
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (n,))
    conn.commit()
    conn.close()
    up.message.reply_text(f"✅ Category '{n}' added")
    return -1

def lcat(up, ctx):
    q = up.callback_query
    q.answer()
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM categories")
    cats = c.fetchall()
    conn.close()
    if not cats:
        q.edit_message_text("📭 No categories")
        return
    kb = []
    for c in cats:
        kb.append([
            InlineKeyboardButton(f"📁 {c['name']}", cb=f"catadm_{c['id']}"),
            InlineKeyboardButton("✏️", cb=f"ecat_{c['id']}"),
            InlineKeyboardButton("🗑️", cb=f"dcat_{c['id']}")
        ])
    kb.append([InlineKeyboardButton("🔙 Back", cb="ashop")])
    q.edit_message_text("📂 Categories:", reply_markup=InlineKeyboardMarkup(kb))

def catadm(up, ctx):
    q = up.callback_query
    q.answer()
    cid = int(q.data.split("_")[1])
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM products WHERE cat_id=?", (cid,))
    prods = c.fetchall()
    conn.close()
    kb = []
    for p in prods:
        kb.append([
            InlineKeyboardButton(f"{p['name']} | {p['price']}", cb=f"prod_{p['id']}"),
            InlineKeyboardButton("✏️", cb=f"eprod_{p['id']}"),
            InlineKeyboardButton("🗑️", cb=f"dprod_{p['id']}")
        ])
    kb.append([InlineKeyboardButton("➕ Add Product", cb=f"aprod_{cid}")])
    kb.append([InlineKeyboardButton("🔙 Back", cb="lcat")])
    q.edit_message_text("📦 Products:", reply_markup=InlineKeyboardMarkup(kb))

def ecat(up, ctx):
    q = up.callback_query
    q.answer()
    cid = int(q.data.split("_")[1])
    ctx.user_data['ecat'] = cid
    q.message.reply_text("✏️ New name:")
    return CAT_EDIT

def edit_cat(up, ctx):
    n = up.message.text
    cid = ctx.user_data['ecat']
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE categories SET name=? WHERE id=?", (n, cid))
    conn.commit()
    conn.close()
    up.message.reply_text("✅ Updated")
    return -1

def dcat(up, ctx):
    q = up.callback_query
    q.answer()
    cid = int(q.data.split("_")[1])
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM categories WHERE id=?", (cid,))
    conn.commit()
    conn.close()
    q.edit_message_text("🗑️ Deleted")

def aprod(up, ctx):
    q = up.callback_query
    q.answer()
    cid = int(q.data.split("_")[1])
    ctx.user_data['pcid'] = cid
    q.message.reply_text("📝 Product name:")
    return P_NAME

def pname(up, ctx):
    ctx.user_data['pn'] = up.message.text
    up.message.reply_text("📝 Description:")
    return P_DESC

def pdesc(up, ctx):
    ctx.user_data['pd'] = up.message.text
    up.message.reply_text("💰 Price:")
    return P_PRICE

def pprice(up, ctx):
    try:
        p = float(up.message.text)
        ctx.user_data['pp'] = p
        kb = ReplyKeyboardMarkup([["digital","file","physical"]], one_time=True, resize=True)
        up.message.reply_text("📦 Type:", reply_markup=kb)
        return P_TYPE
    except:
        up.message.reply_text("❌ Invalid")
        return P_PRICE

def ptype(up, ctx):
    t = up.message.text.lower()
    if t not in ('digital','file','physical'):
        up.message.reply_text("❌ Invalid")
        return P_TYPE
    ctx.user_data['pt'] = t
    if t == 'physical':
        ctx.user_data['pc'] = "Physical"
        up.message.reply_text("📦 Stock (-1 unlimited):", reply_markup=ReplyKeyboardMarkup.remove_keyboard())
        return P_STOCK
    up.message.reply_text("📄 Content:", reply_markup=ReplyKeyboardMarkup.remove_keyboard())
    return P_CONTENT

def pcontent(up, ctx):
    if up.message.document:
        c = up.message.document.file_id
    elif up.message.photo:
        c = up.message.photo[-1].file_id
    else:
        c = up.message.text or "None"
    ctx.user_data['pc'] = c
    up.message.reply_text("📦 Stock (-1 unlimited):")
    return P_STOCK

def pstock(up, ctx):
    try:
        s = int(up.message.text)
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("""INSERT INTO products (cat_id,type,name,desc,price,content,stock) 
                    VALUES (?,?,?,?,?,?,?)""",
                  (ctx.user_data['pcid'], ctx.user_data['pt'], ctx.user_data['pn'],
                   ctx.user_data['pd'], ctx.user_data['pp'], ctx.user_data['pc'], s))
        conn.commit()
        conn.close()
        up.message.reply_text("✅ Product added!")
        return -1
    except:
        up.message.reply_text("❌ Invalid")
        return P_STOCK

def eprod(up, ctx):
    q = up.callback_query
    q.answer()
    pid = int(q.data.split("_")[1])
    ctx.user_data['epid'] = pid
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Name", cb="en"), InlineKeyboardButton("Desc", cb="ed")],
        [InlineKeyboardButton("Price", cb="ep"), InlineKeyboardButton("Stock", cb="es")],
        [InlineKeyboardButton("Content", cb="ec"), InlineKeyboardButton("Type", cb="et")]
    ])
    q.message.reply_text("✏️ Edit:", reply_markup=kb)
    return P_TYPE

def efield(up, ctx):
    q = up.callback_query
    q.answer()
    fld = q.data
    ctx.user_data['ef'] = fld
    q.message.reply_text(f"✏️ New value:")
    return P_NAME

def eupdate(up, ctx):
    val = up.message.text
    pid = ctx.user_data['epid']
    fld = {'en':'name','ed':'desc','ep':'price','es':'stock','ec':'content','et':'type'}[ctx.user_data['ef']]
    if fld in ('price','stock'):
        try:
            val = float(val) if fld=='price' else int(val)
        except:
            up.message.reply_text("❌ Invalid")
            return P_NAME
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(f"UPDATE products SET {fld}=? WHERE id=?", (val, pid))
    conn.commit()
    conn.close()
    up.message.reply_text("✅ Updated")
    return -1

def dprod(up, ctx):
    q = up.callback_query
    q.answer()
    pid = int(q.data.split("_")[1])
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM products WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    q.edit_message_text("🗑️ Deleted")

def auser(up, ctx):
    q = up.callback_query
    q.answer()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Search", cb="usearch")],
        [InlineKeyboardButton("📋 List", cb="ulist")]
    ])
    q.edit_message_text("👥 Users", reply_markup=kb)

def usearch(up, ctx):
    q = up.callback_query
    q.answer()
    q.message.reply_text("🆔 ID or @username:")
    return SEARCH

def ushow(up, ctx):
    q = up.message.text
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
        up.message.reply_text("❌ Not found")
        return -1
    txt = f"""👤 User
ID: `{u['user_id']}`
Name: {u['full_name']}
@{u['username']}
💰 {u['balance']}
Banned: {u['banned']}"""
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Give", cb=f"give_{u['user_id']}"),
         InlineKeyboardButton("🚫 Ban", cb=f"ban_{u['user_id']}")]
    ])
    up.message.reply_text(txt, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    return -1

def give(up, ctx):
    q = up.callback_query
    q.answer()
    uid = int(q.data.split("_")[1])
    ctx.user_data['give_uid'] = uid
    q.message.reply_text("💰 Amount (+/-):")
    return BALANCE

def balance(up, ctx):
    try:
        amt = float(up.message.text)
        uid = ctx.user_data['give_uid']
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amt, uid))
        conn.commit()
        conn.close()
        up.message.reply_text(f"✅ Updated by {amt}")
        return -1
    except:
        up.message.reply_text("❌ Invalid")
        return BALANCE

def ban(up, ctx):
    q = up.callback_query
    q.answer()
    uid = int(q.data.split("_")[1])
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT banned FROM users WHERE user_id=?", (uid,))
    r = c.fetchone()
    new = 0 if r[0] else 1
    c.execute("UPDATE users SET banned=? WHERE user_id=?", (new, uid))
    conn.commit()
    conn.close()
    q.edit_message_text(f"User {'banned' if new else 'unbanned'}")

def ulist(up, ctx):
    q = up.callback_query
    q.answer()
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT user_id,full_name,balance FROM users LIMIT 20")
    us = c.fetchall()
    conn.close()
    txt = "👥 Users:\n"
    for u in us:
        txt += f"\n`{u['user_id']}` | {u['full_name']} | {u['balance']}"
    q.edit_message_text(txt)

def aset(up, ctx):
    q = up.callback_query
    q.answer()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Welcome",cb="set_welcome"), InlineKeyboardButton("Currency",cb="set_currency")],
        [InlineKeyboardButton("Support",cb="set_support"), InlineKeyboardButton("Rules",cb="set_rules")],
        [InlineKeyboardButton("Channel",cb="set_channel"), InlineKeyboardButton("Captcha",cb="set_captcha")],
        [InlineKeyboardButton("Shop",cb="set_shop"), InlineKeyboardButton("Referral",cb="set_referral")],
        [InlineKeyboardButton("Daily Amt",cb="set_daily_amt"), InlineKeyboardButton("Daily On",cb="set_daily_on")],
        [InlineKeyboardButton("Scratch Rew",cb="set_scratch_rew"), InlineKeyboardButton("Scratch On",cb="set_scratch_on")],
    ])
    q.edit_message_text("⚙️ Settings", reply_markup=kb)

def set_cb(up, ctx):
    q = up.callback_query
    q.answer()
    key = q.data.replace("set_","")
    ctx.user_data['set_key'] = key
    q.message.reply_text(f"✏️ New value for {key}:")
    return SET_VAL

def set_val(up, ctx):
    val = up.message.text
    key = ctx.user_data['set_key']
    set_setting(key, val)
    up.message.reply_text(f"✅ {key} updated")
    return -1

def apromo(up, ctx):
    q = up.callback_query
    q.answer()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Create", cb="cpromo")],
        [InlineKeyboardButton("📋 List", cb="lpromo")]
    ])
    q.edit_message_text("🎁 Promos", reply_markup=kb)

def cpromo(up, ctx):
    q = up.callback_query
    q.answer()
    q.message.reply_text("📝 Code:")
    return PROMO

def promocode(up, ctx):
    ctx.user_data['pcode'] = up.message.text
    up.message.reply_text("💰 Reward:")
    return PROMO_REW

def promorew(up, ctx):
    try:
        r = float(up.message.text)
        ctx.user_data['prew'] = r
        up.message.reply_text("🔢 Max uses (-1=∞):")
        return PROMO_LIM
    except:
        up.message.reply_text("❌ Invalid")
        return PROMO_REW

def promolim(up, ctx):
    try:
        l = int(up.message.text)
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("INSERT INTO promos (code,reward,max_use,expiry) VALUES (?,?,?,?)",
                  (ctx.user_data['pcode'], ctx.user_data['prew'], l, "2099-12-31"))
        conn.commit()
        conn.close()
        up.message.reply_text("✅ Promo created")
        return -1
    except:
        up.message.reply_text("❌ Invalid")
        return PROMO_LIM

def lpromo(up, ctx):
    q = up.callback_query
    q.answer()
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM promos")
    ps = c.fetchall()
    conn.close()
    if not ps:
        q.edit_message_text("📭 No promos")
        return
    for p in ps:
        txt = f"`{p['code']}` | {p['reward']} | {p['used']}/{p['max_use']} | {p['expiry']}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🗑️", cb=f"dpromo_{p['code']}")]])
        ctx.bot.send_message(q.from_user.id, txt, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

def dpromo(up, ctx):
    q = up.callback_query
    q.answer()
    code = q.data.split("_")[1]
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM promos WHERE code=?", (code,))
    conn.commit()
    conn.close()
    q.edit_message_text("🗑️ Deleted")

def atask(up, ctx):
    q = up.callback_query
    q.answer()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Create", cb="ctask")],
        [InlineKeyboardButton("📋 List", cb="ltask")]
    ])
    q.edit_message_text("📋 Tasks", reply_markup=kb)

def ctask(up, ctx):
    q = up.callback_query
    q.answer()
    q.message.reply_text("📝 Description:")
    return TASK_DESC

def taskdesc(up, ctx):
    ctx.user_data['td'] = up.message.text
    up.message.reply_text("🔗 Link (or None):")
    return TASK_LINK

def tasklink(up, ctx):
    ctx.user_data['tl'] = up.message.text
    up.message.reply_text("💰 Reward:")
    return TASK_REW

def taskrew(up, ctx):
    try:
        r = float(up.message.text)
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("INSERT INTO tasks (desc,link,reward) VALUES (?,?,?)",
                  (ctx.user_data['td'], ctx.user_data['tl'], r))
        conn.commit()
        conn.close()
        up.message.reply_text("✅ Task created")
        return -1
    except:
        up.message.reply_text("❌ Invalid")
        return TASK_REW

def ltask(up, ctx):
    q = up.callback_query
    q.answer()
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM tasks")
    ts = c.fetchall()
    conn.close()
    if not ts:
        q.edit_message_text("📭 No tasks")
        return
    for t in ts:
        txt = f"ID: {t['id']}\n{t['desc']}\n{t['link']}\nReward: {t['reward']}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🗑️", cb=f"dtask_{t['id']}")]])
        ctx.bot.send_message(q.from_user.id, txt, reply_markup=kb)

def dtask(up, ctx):
    q = up.callback_query
    q.answer()
    tid = int(q.data.split("_")[1])
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM tasks WHERE id=?", (tid,))
    conn.commit()
    conn.close()
    q.edit_message_text("🗑️ Deleted")

def aorder(up, ctx):
    q = up.callback_query
    q.answer()
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT o.*, p.name FROM orders o JOIN products p ON o.product_id=p.id WHERE status='pending'")
    os = c.fetchall()
    conn.close()
    if not os:
        q.edit_message_text("📭 No pending")
        return
    for o in os:
        txt = f"📦 Order #{o['id']}\n{o['name']}\nUser: {o['user_id']}\n{o['data']}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Deliver", cb=f"deliver_{o['id']}")]])
        ctx.bot.send_message(q.from_user.id, txt, reply_markup=kb)

def deliver(up, ctx):
    q = up.callback_query
    q.answer()
    oid = int(q.data.split("_")[1])
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE orders SET status='delivered' WHERE id=?", (oid,))
    conn.commit()
    conn.close()
    q.edit_message_text(f"✅ Order #{oid} delivered")

def apanel(up, ctx):
    q = up.callback_query
    q.answer()
    q.edit_message_text("🔧 Admin", reply_markup=admin_kb())

def cancel(up, ctx):
    up.message.reply_text("❌ Cancelled")
    return -1

# ---------- Main ----------
def main():
    init_db()
    threading.Thread(target=run_flask, daemon=True).start()
    
    upd = Updater(TOKEN, use_context=True)
    dp = upd.dispatcher
    
    # Conversation
    dp.add_handler(ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={CAPTCHA: [MessageHandler(Filters.text, captcha)]},
        fallbacks=[CommandHandler('cancel', cancel)]
    ))
    dp.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(promo_ask, pattern='^promo$')],
        states={PROMO: [MessageHandler(Filters.text, promo_use)]},
        fallbacks=[CommandHandler('cancel', cancel)]
    ))
    dp.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(buy, pattern='^buy_')],
        states={ADDR: [MessageHandler(Filters.text, addr_save)]},
        fallbacks=[CommandHandler('cancel', cancel)]
    ))
    dp.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(acat, pattern='^acat$')],
        states={CAT_NAME: [MessageHandler(Filters.text, add_cat)]},
        fallbacks=[CommandHandler('cancel', cancel)]
    ))
    dp.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(ecat, pattern='^ecat_')],
        states={CAT_EDIT: [MessageHandler(Filters.text, edit_cat)]},
        fallbacks=[CommandHandler('cancel', cancel)]
    ))
    dp.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(aprod, pattern='^aprod_')],
        states={
            P_NAME: [MessageHandler(Filters.text, pname)],
            P_DESC: [MessageHandler(Filters.text, pdesc)],
            P_PRICE: [MessageHandler(Filters.text, pprice)],
            P_TYPE: [MessageHandler(Filters.text, ptype)],
            P_CONTENT: [MessageHandler(Filters.all, pcontent)],
            P_STOCK: [MessageHandler(Filters.text, pstock)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    ))
    dp.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(eprod, pattern='^eprod_')],
        states={
            P_TYPE: [CallbackQueryHandler(efield, pattern='^e')],
            P_NAME: [MessageHandler(Filters.text, eupdate)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    ))
    dp.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(usearch, pattern='^usearch$')],
        states={SEARCH: [MessageHandler(Filters.text, ushow)]},
        fallbacks=[CommandHandler('cancel', cancel)]
    ))
    dp.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(give, pattern='^give_')],
        states={BALANCE: [MessageHandler(Filters.text, balance)]},
        fallbacks=[CommandHandler('cancel', cancel)]
    ))
    dp.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(set_cb, pattern='^set_')],
        states={SET_VAL: [MessageHandler(Filters.text, set_val)]},
        fallbacks=[CommandHandler('cancel', cancel)]
    ))
    dp.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(cpromo, pattern='^cpromo$')],
        states={
            PROMO: [MessageHandler(Filters.text, promocode)],
            PROMO_REW: [MessageHandler(Filters.text, promorew)],
            PROMO_LIM: [MessageHandler(Filters.text, promolim)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    ))
    dp.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(ctask, pattern='^ctask$')],
        states={
            TASK_DESC: [MessageHandler(Filters.text, taskdesc)],
            TASK_LINK: [MessageHandler(Filters.text, tasklink)],
            TASK_REW: [MessageHandler(Filters.text, taskrew)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    ))
    
    # Message handlers
    dp.add_handler(MessageHandler(Filters.text & Filters.regex('^👤 Profile$'), profile))
    dp.add_handler(MessageHandler(Filters.text & Filters.regex('^🎁 Daily$'), daily))
    dp.add_handler(MessageHandler(Filters.text & Filters.regex('^🎲 Scratch$'), scratch))
    dp.add_handler(MessageHandler(Filters.text & Filters.regex('^📋 Tasks$'), tasks))
    dp.add_handler(MessageHandler(Filters.text & Filters.regex('^ℹ️ Support$'), support))
    dp.add_handler(MessageHandler(Filters.text & Filters.regex('^📜 Rules$'), rules))
    dp.add_handler(MessageHandler(Filters.text & Filters.regex('^🛍 Shop$'), shop))
    dp.add_handler(MessageHandler(Filters.text & Filters.regex('^⚙️ Admin$'), admin))
    
    # Callback handlers
    for p,r in [
        ('myhist', my_hist), ('myord', my_orders), ('dotask_', do_task),
        ('cat_', cat_prod), ('prod_', prod_detail), ('back_', back),
        ('astat', astats), ('abackup', abackup), ('abcast', abcast),
        ('ashop', ashop), ('lcat', lcat), ('catadm_', catadm),
        ('dcat_', dcat), ('dprod_', dprod), ('auser', auser),
        ('ulist', ulist), ('ban_', ban), ('aset', aset),
        ('apromo', apromo), ('lpromo', lpromo), ('dpromo_', dpromo),
        ('atask', atask), ('ltask', ltask), ('dtask_', dtask),
        ('aorder', aorder), ('deliver_', deliver), ('apanel', apanel),
    ]:
        dp.add_handler(CallbackQueryHandler(r, pattern=f'^{p}'))
    
    upd.start_polling()
    print(f"✅ Bot running on port {PORT}")
    upd.idle()

if __name__ == "__main__":
    main()
