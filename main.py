#!/usr/bin/env python3
"""
VOLTX BOT - SEVEN Brand OTP Bot
All-in-one single file - async, fast, optimized
"""

import asyncio
import sqlite3
import re
import os
import logging
from datetime import datetime, timedelta, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
import aiohttp
import uuid

# ════════════════════════════════════════════════════════════
# SHARED HTTP SESSION
# ════════════════════════════════════════════════════════════

_http_session: aiohttp.ClientSession = None


async def _get_session():
    global _http_session
    if _http_session is None:
        conn = aiohttp.TCPConnector(limit=1000, limit_per_host=1000, ttl_dns_cache=300)
        _http_session = aiohttp.ClientSession(
            connector=conn,
            timeout=aiohttp.ClientTimeout(total=15)
        )
    return _http_session


# ════════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════════
BOT_TOKEN = os.getenv("BOT_TOKEN", "8805015417:AAFfEZl0aeej1wpECLRapLblP9r3pRCO7M0")
DEFAULT_ADMIN = os.getenv("ADMIN_UID", "6668016879")
API_BASE = "https://api.2oo9.cloud/MXS47FLFX0U/tnevs/@public/api"
DEFAULT_GROUP_ID = -1003727266573
DB_FILE = "data.db"
POLL_INTERVAL = 0.25
OTP_TIMEOUT = 10
DEFAULT_BRAND = "━━〔 SEVEN 〕━━"

logging.basicConfig(
    format="%(asctime)s | %(levelname)-5s | %(message)s",
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logger = logging.getLogger("VOLTX")

_user_states = {}
_tracked_msgs: dict[int, set[int]] = {}  # uid → set of msg_ids with buttons
_active_tasks: dict[int, asyncio.Task] = {}  # key = row_id
_active_stop: dict[int, asyncio.Event] = {}  # key = row_id
_number_msg_id: dict[int, int] = {}  # row_id → msg_id of that number's waiting message


async def _rm_buttons(uid, context):
    mids = _tracked_msgs.pop(uid, None)
    if not mids:
        return
    for mid in mids:
        try:
            await context.bot.edit_message_reply_markup(chat_id=uid, message_id=mid, reply_markup=None)
        except Exception:
            pass


async def _rm_number_msg(row_id, uid, context):
    mid = _number_msg_id.pop(row_id, None)
    if mid is None:
        return
    ts = _tracked_msgs.get(uid)
    if ts:
        ts.discard(mid)
    try:
        await context.bot.edit_message_reply_markup(chat_id=uid, message_id=mid, reply_markup=None)
    except Exception:
        pass


def _track_msg(uid, msg_id):
    _tracked_msgs.setdefault(uid, set()).add(msg_id)


# ════════════════════════════════════════════════════════════
# DATABASE
# ════════════════════════════════════════════════════════════

def _conn():
    c = sqlite3.connect(DB_FILE, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    return c


def init_db():
    c = _conn()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            uid INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            is_banned INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS countries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            flag TEXT DEFAULT '',
            range_id TEXT NOT NULL,
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS active_numbers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid INTEGER NOT NULL,
            country_id INTEGER NOT NULL,
            number TEXT NOT NULL DEFAULT '',
            full_number TEXT DEFAULT '',
            national_number TEXT DEFAULT '',
            operator TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            expires_at TEXT DEFAULT '',
            otp TEXT DEFAULT '',
            otp_received INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_active_uid ON active_numbers(uid);
        CREATE INDEX IF NOT EXISTS idx_active_otp ON active_numbers(otp_received);
    """)
    for col in ("api_number_id", "task_id"):
        try:
            c.execute(f"ALTER TABLE active_numbers ADD COLUMN {col} TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
    try:
        c.execute("CREATE INDEX IF NOT EXISTS idx_active_api ON active_numbers(api_number_id)")
    except sqlite3.OperationalError:
        pass
    defaults = {
        "brand_name": DEFAULT_BRAND,
        "number_hide": "1",
        "api_key": "",
        "admin_uids": DEFAULT_ADMIN,
        "group_id": str(DEFAULT_GROUP_ID),
    }
    for k, v in defaults.items():
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
    c.commit()
    c.close()


def get_setting(key, default=""):
    c = _conn()
    r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    c.close()
    return r["value"] if r else default


def set_setting(key, value):
    c = _conn()
    c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, str(value)))
    c.commit()
    c.close()


def is_admin(uid):
    raw = get_setting("admin_uids", "")
    if not raw:
        return False
    try:
        return uid in [int(x.strip()) for x in raw.split(",") if x.strip()]
    except ValueError:
        return False


def register_user(uid, username):
    c = _conn()
    c.execute("INSERT OR IGNORE INTO users(uid,username) VALUES(?,?)", (uid, username))
    c.commit()
    c.close()


def is_banned(uid):
    c = _conn()
    r = c.execute("SELECT is_banned FROM users WHERE uid=?", (uid,)).fetchone()
    c.close()
    return bool(r and r["is_banned"] == 1)


def get_active_countries():
    c = _conn()
    rows = c.execute("SELECT * FROM countries WHERE is_active=1 ORDER BY id").fetchall()
    c.close()
    return rows


def get_all_countries():
    c = _conn()
    rows = c.execute("SELECT * FROM countries ORDER BY id").fetchall()
    c.close()
    return rows


def get_all_user_ids():
    c = _conn()
    rows = c.execute("SELECT uid FROM users WHERE is_banned=0").fetchall()
    c.close()
    return [r["uid"] for r in rows]


def store_number(uid, country_id, data):
    c = _conn()
    now = datetime.now(timezone.utc)
    exp = now + timedelta(minutes=OTP_TIMEOUT)
    api_id = data.get("id", "") or data.get("number_id", "") or data.get("api_number_id", "") or ""
    task_id = uuid.uuid4().hex[:8]
    cur = c.execute(
        """INSERT INTO active_numbers
           (uid,country_id,number,full_number,national_number,operator,created_at,expires_at,api_number_id,task_id)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (uid, country_id,
         data.get("no_plus_number", ""),
         data.get("full_number", ""),
         data.get("national_number", ""),
         data.get("operator", ""),
         now.isoformat(),
         exp.isoformat(),
         api_id,
         task_id)
    )
    row_id = cur.lastrowid
    c.commit()
    c.close()
    logger.info("Stored number row_id=%s api_id=%s task_id=%s", row_id, api_id, task_id)
    return row_id


def get_user_active(uid):
    c = _conn()
    r = c.execute(
        """SELECT a.*, c.name AS cname, c.flag AS cflag
           FROM active_numbers a JOIN countries c ON a.country_id=c.id
           WHERE a.uid=? AND a.otp_received=0
           ORDER BY a.id DESC LIMIT 1""",
        (uid,)
    ).fetchone()
    c.close()
    return r


def get_all_active():
    c = _conn()
    rows = c.execute(
        """SELECT a.*, c.name AS cname, c.flag AS cflag
           FROM active_numbers a JOIN countries c ON a.country_id=c.id
           WHERE a.otp_received=0"""
    ).fetchall()
    c.close()
    return rows


def delete_user_active(uid):
    c = _conn()
    c.execute("DELETE FROM active_numbers WHERE uid=? AND otp_received=0", (uid,))
    c.commit()
    c.close()


def delete_number_by_id(rid):
    c = _conn()
    c.execute("DELETE FROM active_numbers WHERE id=?", (rid,))
    c.commit()
    c.close()


def mark_otp(rid, otp):
    c = _conn()
    c.execute("UPDATE active_numbers SET otp=?,otp_received=1 WHERE id=?", (otp, rid))
    c.commit()
    c.close()


# ════════════════════════════════════════════════════════════
# TASK MANAGEMENT
# ════════════════════════════════════════════════════════════


def _stop_task(row_id):
    stop = _active_stop.pop(row_id, None)
    if stop:
        try:
            stop.set()
        except RuntimeError:
            pass
    task = _active_tasks.pop(row_id, None)
    if task:
        task.cancel()


def _start_task(uid, row_id, context):
    stop = asyncio.Event()
    task = asyncio.create_task(_poll_number_task(uid, row_id, stop, context))
    _active_tasks[row_id] = task
    _active_stop[row_id] = stop


# ════════════════════════════════════════════════════════════
# API CLIENT
# ════════════════════════════════════════════════════════════

async def api_getnum(range_id):
    key = get_setting("api_key")
    if not key:
        return None
    hdr = {"mauthapi": key, "Content-Type": "application/json"}
    try:
        s = await _get_session()
        async with s.post(f"{API_BASE}/getnum", json={"rid": range_id}, headers=hdr) as r:
            d = await r.json()
            if d.get("meta", {}).get("code") == 200:
                return d.get("data")
    except Exception as e:
        logger.error("API getnum failed: %s", e)
    return None


async def api_success_otp():
    key = get_setting("api_key")
    if not key:
        return []
    hdr = {"mauthapi": key}
    try:
        s = await _get_session()
        async with s.get(f"{API_BASE}/success-otp", headers=hdr) as r:
            d = await r.json()
            if d.get("meta", {}).get("code") == 200:
                return d.get("data", {}).get("otps", [])
    except Exception as e:
        logger.error("API success-otp failed: %s", e)
    return []


async def api_console():
    key = get_setting("api_key")
    if not key:
        return []
    hdr = {"mauthapi": key}
    try:
        s = await _get_session()
        async with s.get(f"{API_BASE}/console", headers=hdr) as r:
            d = await r.json()
            if d.get("meta", {}).get("code") == 200:
                return d.get("data", {}).get("hits", [])
    except Exception as e:
        logger.error("API console failed: %s", e)
    return []


# ════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════

def hide_number(number, brand):
    n = number.replace("+", "").replace(" ", "")
    if len(n) < 7:
        return brand
    return f"{n[:3]}{brand}{n[-3:]}"


def extract_otp(msg):
    m = re.search(r"(\d{4,8})", msg)
    return m.group(1) if m else msg


def _brand():
    return get_setting("brand_name", DEFAULT_BRAND)


def _num_hide():
    return get_setting("number_hide") == "1"


def _clean(num):
    return (num or "").replace("+", "").replace(" ", "")


def _sms_buttons():
    return [
        [
            InlineKeyboardButton("🔢 New Number", callback_data="btn_new"),
            InlineKeyboardButton("📨 OTP Group", url="https://t.me/seven_otp"),
        ],
        [InlineKeyboardButton("🌍 Change Country", callback_data="btn_chg")],
    ]


def _back_btn(cb="back_start"):
    return InlineKeyboardButton("🔙 Back", callback_data=cb)


# ════════════════════════════════════════════════════════════
# SHOW FUNCTIONS
# ════════════════════════════════════════════════════════════

async def show_start(target):
    b = _brand()
    countries = get_active_countries()
    if not countries:
        await target(f"{b}\n\nNo countries available yet.")
        return
    text = f"━━━━━━━━━━━━━━━━\n{b}\n━━━━━━━━━━━━━━━━\n\n🌍 Select a country:"
    btns = [[InlineKeyboardButton(f"{c['flag']} {c['name']}", callback_data=f"sel_{c['id']}")] for c in countries]
    await target(text, reply_markup=InlineKeyboardMarkup(btns))


async def show_countries_edit(message):
    b = _brand()
    countries = get_active_countries()
    if not countries:
        return await message.reply_text("No countries available.")
    text = f"{b}\n\n🌍 Select a country:"
    btns = [[InlineKeyboardButton(f"{c['flag']} {c['name']}", callback_data=f"sel_{c['id']}")] for c in countries]
    return await message.reply_text(text, reply_markup=InlineKeyboardMarkup(btns))


# ════════════════════════════════════════════════════════════
# ADMIN PANEL
# ════════════════════════════════════════════════════════════

async def show_admin(target, edit=False):
    b = _brand()
    nh = "ON" if get_setting("number_hide") == "1" else "OFF"
    ak = get_setting("api_key")
    ak_d = (ak[:12] + "...") if len(ak) > 12 else (ak or "Not set")
    text = f"""{b} — Admin Panel

👁 Number Hide: {nh}
🔑 API Key: {ak_d}"""
    btns = [
        [InlineKeyboardButton("🔨 Ban/Unban", callback_data="adm_ban")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="adm_brd")],
        [InlineKeyboardButton("🌍 Country Management", callback_data="adm_ctr")],
        [InlineKeyboardButton("➕ Add Country", callback_data="adm_add")],
        [InlineKeyboardButton("✏️ Brand Name", callback_data="adm_bnd")],
        [InlineKeyboardButton(f"👁 Number Hide: {nh}", callback_data="adm_hde")],
        [InlineKeyboardButton("🔑 Set API Key", callback_data="adm_api")],
    ]
    if edit:
        await target.edit_text(text, reply_markup=InlineKeyboardMarkup(btns))
    else:
        await target(text, reply_markup=InlineKeyboardMarkup(btns))


async def show_admin_countries(message, edit=False):
    countries = get_all_countries()
    if not countries:
        text = "No countries added yet."
        btns = [
            [InlineKeyboardButton("➕ Add Country", callback_data="adm_add")],
            [_back_btn("adm_menu")],
        ]
    else:
        text = "🌍 Country Management:"
        btns = []
        for c in countries:
            st = "🟢" if c["is_active"] else "🔴"
            btns.append([
                InlineKeyboardButton(
                    f"{st} {c['flag']} {c['name']} [{c['range_id']}]",
                    callback_data=f"cnoop_{c['id']}"
                )
            ])
            btns.append([
                InlineKeyboardButton("✏️ Edit", callback_data=f"cedt_{c['id']}"),
                InlineKeyboardButton(
                    "🔴 Off" if c["is_active"] else "🟢 On",
                    callback_data=f"ctgl_{c['id']}"
                ),
                InlineKeyboardButton("🗑 Delete", callback_data=f"cdel_{c['id']}"),
            ])
        btns.append([InlineKeyboardButton("➕ Add Country", callback_data="adm_add")])
        btns.append([_back_btn("adm_menu")])
    if edit:
        await message.edit_text(text, reply_markup=InlineKeyboardMarkup(btns))
    else:
        await message.reply_text(text, reply_markup=InlineKeyboardMarkup(btns))


# ════════════════════════════════════════════════════════════
# BOT COMMANDS
# ════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    uname = update.effective_user.username or ""
    register_user(uid, uname)
    if is_banned(uid):
        return await update.message.reply_text("⛔ You are banned from using this bot.")
    await show_start(update.message.reply_text)


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return await update.message.reply_text("⛔ Access denied.")
    await show_admin(update.message.reply_text)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    _user_states.pop(uid, None)
    await update.message.reply_text("Cancelled.")


# ════════════════════════════════════════════════════════════
# CALLBACK HANDLER
# ════════════════════════════════════════════════════════════

async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    data = q.data
    await q.answer()

    if is_banned(uid):
        return await q.edit_message_text("⛔ You are banned.")

    _user_states.pop(uid, None)
    await _rm_buttons(uid, context)

    # ──────── ADMIN ────────

    if data == "adm_menu":
        return await show_admin(q.message, edit=True)

    if data == "adm_ban":
        _user_states[uid] = {"s": "ban"}
        return await q.edit_message_text("Send UID to ban/unban (number only):")

    if data == "adm_brd":
        _user_states[uid] = {"s": "brc"}
        return await q.edit_message_text("Send broadcast message:")

    if data == "adm_ctr":
        return await show_admin_countries(q.message, edit=True)

    if data == "adm_add":
        _user_states[uid] = {"s": "add_name"}
        return await q.edit_message_text(
            "Send country name and flag:\nExample: `Bangladesh 🇧🇩`",
            parse_mode="Markdown"
        )

    if data == "adm_bnd":
        _user_states[uid] = {"s": "bnd"}
        return await q.edit_message_text("Send new brand name:")

    if data == "adm_hde":
        cur = get_setting("number_hide")
        new = "0" if cur == "1" else "1"
        set_setting("number_hide", new)
        st = "ON" if new == "1" else "OFF"
        return await q.edit_message_text(
            f"👁 Number Hide: {st}",
            reply_markup=InlineKeyboardMarkup([[_back_btn("adm_menu")]])
        )

    if data == "adm_api":
        _user_states[uid] = {"s": "api"}
        return await q.edit_message_text("Send the API key (mauthapi):")

    if data.startswith("ctgl_"):
        cid = int(data.split("_")[1])
        conn = _conn()
        r = conn.execute("SELECT is_active FROM countries WHERE id=?", (cid,)).fetchone()
        if r:
            conn.execute("UPDATE countries SET is_active=? WHERE id=?", (0 if r["is_active"] else 1, cid))
            conn.commit()
        conn.close()
        return await show_admin_countries(q.message, edit=True)

    if data.startswith("cdel_"):
        cid = int(data.split("_")[1])
        conn = _conn()
        conn.execute("DELETE FROM countries WHERE id=?", (cid,))
        conn.commit()
        conn.close()
        return await show_admin_countries(q.message, edit=True)

    if data.startswith("cedt_"):
        cid = int(data.split("_")[1])
        conn = _conn()
        r = conn.execute("SELECT * FROM countries WHERE id=?", (cid,)).fetchone()
        conn.close()
        if r:
            _user_states[uid] = {"s": "edt", "cid": cid}
            return await q.edit_message_text(
                f"Editing: {r['flag']} {r['name']} [{r['range_id']}]\n\n"
                f"Send: `Name Flag RangeID`\n(separate with spaces)",
                parse_mode="Markdown"
            )
        return

    if data.startswith("cnoop_"):
        return

    # ──────── USER ────────

    if data.startswith("sel_"):
        cid = int(data.split("_")[1])
        delete_user_active(uid)
        conn = _conn()
        c = conn.execute("SELECT * FROM countries WHERE id=? AND is_active=1", (cid,)).fetchone()
        conn.close()
        if not c:
            return await q.message.reply_text("Country unavailable.")
        load_msg = await q.message.reply_text(f"⏳ Getting number from {c['flag']} {c['name']}...")
        num = await api_getnum(c["range_id"])
        if not num:
            await load_msg.edit_text(
                "❌ No numbers available. Try another country.",
                reply_markup=InlineKeyboardMarkup([[_back_btn("back_start")]])
            )
            _track_msg(uid, load_msg.message_id)
            return
        row_id = store_number(uid, cid, num)
        _start_task(uid, row_id, context)
        flag = c["flag"] or ""
        name = c["name"]
        number = _clean(num.get("full_number", ""))
        text = f"☎️ {flag} {name} | <code>{number}</code> | 🔑 | -----"
        await load_msg.edit_text(text, reply_markup=InlineKeyboardMarkup(_sms_buttons()), parse_mode="HTML")
        _track_msg(uid, load_msg.message_id)
        _number_msg_id[row_id] = load_msg.message_id
        return

    if data == "btn_new":
        active = get_user_active(uid)
        if active:
            cid = active["country_id"]
            conn = _conn()
            c = conn.execute("SELECT * FROM countries WHERE id=? AND is_active=1", (cid,)).fetchone()
            conn.close()
            if c:
                load_msg = await q.message.reply_text(f"⏳ Getting number from {c['flag']} {c['name']}...")
                num = await api_getnum(c["range_id"])
                if num:
                    row_id = store_number(uid, cid, num)
                    _start_task(uid, row_id, context)
                    flag = c["flag"] or ""
                    name = c["name"]
                    number = _clean(num.get("full_number", ""))
                    text = f"☎️ {flag} {name} | <code>{number}</code> | 🔑 | -----"
                    await load_msg.edit_text(
                        text, reply_markup=InlineKeyboardMarkup(_sms_buttons()), parse_mode="HTML"
                    )
                    _track_msg(uid, load_msg.message_id)
                    _number_msg_id[row_id] = load_msg.message_id
                    return
                await load_msg.edit_text("❌ No numbers available. Try another country.")
                _track_msg(uid, load_msg.message_id)
                return
        msg = await show_countries_edit(q.message)
        _track_msg(uid, msg.message_id)
        return

    if data == "btn_chg":
        msg = await show_countries_edit(q.message)
        _track_msg(uid, msg.message_id)

    if data == "back_start":
        msg = await show_countries_edit(q.message)
        _track_msg(uid, msg.message_id)
        return


# ════════════════════════════════════════════════════════════
# TEXT MESSAGE HANDLER (admin states)
# ════════════════════════════════════════════════════════════

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text

    if uid not in _user_states:
        return

    st = _user_states[uid]["s"]

    # ── ban ──
    if st == "ban":
        try:
            tid = int(text.strip())
        except ValueError:
            return await update.message.reply_text("Invalid UID. Send a number.")
        conn = _conn()
        r = conn.execute("SELECT is_banned FROM users WHERE uid=?", (tid,)).fetchone()
        if r:
            new = 0 if r["is_banned"] else 1
            conn.execute("UPDATE users SET is_banned=? WHERE uid=?", (new, tid))
            conn.commit()
            label = "BANNED" if new else "UNBANNED"
            await update.message.reply_text(
                f"✅ User {tid} {label}.",
                reply_markup=InlineKeyboardMarkup([[_back_btn("adm_menu")]])
            )
        else:
            await update.message.reply_text(
                "User not found.",
                reply_markup=InlineKeyboardMarkup([[_back_btn("adm_menu")]])
            )
        conn.close()
        _user_states.pop(uid, None)
        return

    # ── broadcast ──
    if st == "brc":
        uids = get_all_user_ids()
        sent = 0
        for u in uids:
            try:
                await context.bot.send_message(u, f"📢 **Broadcast**\n\n{text}", parse_mode="Markdown")
                sent += 1
            except Exception:
                pass
        await update.message.reply_text(
            f"✅ Sent to {sent}/{len(uids)} users.",
            reply_markup=InlineKeyboardMarkup([[_back_btn("adm_menu")]])
        )
        _user_states.pop(uid, None)
        return

    # ── add country step 1: name+flag ──
    if st == "add_name":
        parts = text.strip().split(None, 1)
        name = parts[0]
        flag = parts[1] if len(parts) > 1 else ""
        _user_states[uid] = {"s": "add_rid", "name": name, "flag": flag}
        await update.message.reply_text(
            f"Country: {flag} {name}\n\nNow send the range_id (digits, no XXX):"
        )
        return

    # ── add country step 2: range_id ──
    if st == "add_rid":
        rid = text.strip()
        name = _user_states[uid]["name"]
        flag = _user_states[uid]["flag"]
        conn = _conn()
        conn.execute("INSERT INTO countries(name,flag,range_id) VALUES(?,?,?)", (name, flag, rid))
        conn.commit()
        conn.close()
        await update.message.reply_text(
            f"✅ Added: {flag} {name} (range: {rid})",
            reply_markup=InlineKeyboardMarkup([[_back_btn("adm_menu")]])
        )
        _user_states.pop(uid, None)
        return

    # ── brand name ──
    if st == "bnd":
        set_setting("brand_name", text.strip())
        await update.message.reply_text(
            f"✅ Brand name updated to:\n{text.strip()}",
            reply_markup=InlineKeyboardMarkup([[_back_btn("adm_menu")]])
        )
        _user_states.pop(uid, None)
        return

    # ── api key ──
    if st == "api":
        set_setting("api_key", text.strip())
        await update.message.reply_text(
            "✅ API key updated.",
            reply_markup=InlineKeyboardMarkup([[_back_btn("adm_menu")]])
        )
        _user_states.pop(uid, None)
        return

    # ── edit country ──
    if st == "edt":
        cid = _user_states[uid].get("cid")
        parts = text.strip().split(None, 2)
        if len(parts) < 2:
            return await update.message.reply_text("Send at least: `Name Flag`", parse_mode="Markdown")
        name = parts[0]
        flag = parts[1]
        rid = parts[2] if len(parts) > 2 else None
        conn = _conn()
        if rid:
            conn.execute("UPDATE countries SET name=?,flag=?,range_id=? WHERE id=?", (name, flag, rid, cid))
        else:
            conn.execute("UPDATE countries SET name=?,flag=? WHERE id=?", (name, flag, cid))
        conn.commit()
        conn.close()
        await update.message.reply_text(
            f"✅ Updated: {flag} {name}",
            reply_markup=InlineKeyboardMarkup([[_back_btn("adm_ctr")]])
        )
        _user_states.pop(uid, None)
        return


# ════════════════════════════════════════════════════════════
# PER-NUMBER OTP POLLING TASK
# ════════════════════════════════════════════════════════════

async def _poll_number_task(uid: int, row_id: int, stop: asyncio.Event, context: ContextTypes.DEFAULT_TYPE):
    seen_otps = set()

    try:
        while not stop.is_set():
            try:
                conn = _conn()
                row = conn.execute(
                    "SELECT a.*, c.name AS cname, c.flag AS cflag "
                    "FROM active_numbers a JOIN countries c ON a.country_id=c.id WHERE a.id=?",
                    (row_id,)
                ).fetchone()
                conn.close()

                if not row:
                    return

                if row["expires_at"]:
                    expires_at = datetime.fromisoformat(row["expires_at"])
                    if datetime.now(timezone.utc) >= expires_at:
                        await _rm_number_msg(row_id, uid, context)
                        conn = _conn()
                        conn.execute("DELETE FROM active_numbers WHERE id=?", (row_id,))
                        conn.commit()
                        conn.close()
                        return

                api_id = row["api_number_id"] or ""
                otps = await api_success_otp()
                for hit in otps:
                    otp_id = hit.get("otp_id", "")
                    if otp_id in seen_otps:
                        continue
                    seen_otps.add(otp_id)

                    hit_api_id = hit.get("id", "") or hit.get("number_id", "") or hit.get("api_number_id", "") or ""
                    hit_num = _clean(hit.get("number", ""))
                    hit_msg = hit.get("message", "")
                    otp_code = extract_otp(hit_msg)

                    nat = _clean(row["national_number"])
                    full = _clean(row["full_number"])

                    matched = False
                    if api_id and hit_api_id and api_id == hit_api_id:
                        matched = True
                    elif hit_num and (hit_num == nat or hit_num == full):
                        matched = True

                    if matched:
                        logger.info("OTP match row_id=%s api=%s number=%s", row_id, api_id, hit_num)
                        mark_otp(row_id, otp_code)
                        display = hide_number(full, _brand()) if _num_hide() else full
                        otp_text_user = f"✅ {row['cflag']} {row['cname']} | <code>{full}</code> | 🔑 | <code>{otp_code}</code>"
                        otp_text_group = f"✅ {row['cflag']} {row['cname']} | <code>{display}</code> | 🔑 | <code>{otp_code}</code>"
                        wmid = _number_msg_id.pop(row_id, None)
                        if wmid:
                            ts = _tracked_msgs.get(uid)
                            if ts:
                                ts.discard(wmid)
                            try:
                                await context.bot.delete_message(chat_id=uid, message_id=wmid)
                            except Exception:
                                pass
                        await _rm_buttons(uid, context)
                        try:
                            msg = await context.bot.send_message(
                                uid, otp_text_user,
                                reply_markup=InlineKeyboardMarkup(_sms_buttons()),
                                parse_mode="HTML"
                            )
                            _track_msg(uid, msg.message_id)
                        except Exception as e:
                            logger.error("Failed to send OTP to user %s: %s", uid, e)
                        try:
                            gid = int(get_setting("group_id", str(DEFAULT_GROUP_ID)))
                        except (ValueError, TypeError):
                            gid = DEFAULT_GROUP_ID
                        try:
                            await context.bot.send_message(gid, otp_text_group, parse_mode="HTML")
                        except Exception as e:
                            logger.error("Failed to send OTP to group %s: %s", gid, e)
                        delete_number_by_id(row_id)
                        return
                
                if not stop.is_set():
                    hits = await api_console()
                    for hit in hits:
                        hit_msg = hit.get("message", "")
                        otp_code = extract_otp(hit_msg)
                        hit_range = hit.get("range", "")
                        hit_time = hit.get("time", 0)
                        key = f"{hit_range}_{hit_time}"
                        if key in seen_otps:
                            continue
                        seen_otps.add(key)
                        
                        nat7 = _clean(row["national_number"])[:7]
                        full9 = _clean(row["full_number"])[:9]
                        if nat7 in hit_range or full9 in hit_range:
                            logger.info("Console match row_id=%s range=%s", row_id, hit_range)
                            mark_otp(row_id, otp_code)
                            clean_full = _clean(row["full_number"])
                            display = hide_number(clean_full, _brand()) if _num_hide() else clean_full
                            otp_text_user = f"✅ {row['cflag']} {row['cname']} | <code>{clean_full}</code> | 🔑 | <code>{otp_code}</code>"
                            otp_text_group = f"✅ {row['cflag']} {row['cname']} | <code>{display}</code> | 🔑 | <code>{otp_code}</code>"
                            wmid = _number_msg_id.pop(row_id, None)
                            if wmid:
                                ts = _tracked_msgs.get(uid)
                                if ts:
                                    ts.discard(wmid)
                                try:
                                    await context.bot.delete_message(chat_id=uid, message_id=wmid)
                                except Exception:
                                    pass
                            await _rm_buttons(uid, context)
                            try:
                                msg = await context.bot.send_message(
                                    uid, otp_text_user,
                                    reply_markup=InlineKeyboardMarkup(_sms_buttons()),
                                    parse_mode="HTML"
                                )
                                _track_msg(uid, msg.message_id)
                            except Exception:
                                pass
                            try:
                                gid = int(get_setting("group_id", str(DEFAULT_GROUP_ID)))
                            except (ValueError, TypeError):
                                gid = DEFAULT_GROUP_ID
                            try:
                                await context.bot.send_message(gid, otp_text_group, parse_mode="HTML")
                            except Exception:
                                pass
                            delete_number_by_id(row_id)
                            return

            except Exception as e:
                logger.error("Poll task [uid=%s] error: %s", uid, e)
                await asyncio.sleep(POLL_INTERVAL)
                continue

            try:
                await asyncio.wait_for(stop.wait(), POLL_INTERVAL)
                return
            except asyncio.TimeoutError:
                continue
    finally:
        if _active_tasks.get(row_id) is asyncio.current_task():
            _active_tasks.pop(row_id, None)
            _active_stop.pop(row_id, None)


async def _recover_tasks(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(timezone.utc).isoformat()
    conn = _conn()
    expired = conn.execute(
        "SELECT id, uid FROM active_numbers WHERE otp_received=0 AND expires_at<?", (now,)
    ).fetchall()
    for exp in expired:
        await _rm_buttons(exp["uid"], context)
        conn.execute("DELETE FROM active_numbers WHERE id=?", (exp["id"],))
    conn.commit()
    active = conn.execute(
        "SELECT a.*, c.name AS cname, c.flag AS cflag "
        "FROM active_numbers a JOIN countries c ON a.country_id=c.id WHERE a.otp_received=0"
    ).fetchall()
    conn.close()
    for row in active:
        rid = row["id"]
        if rid in _active_tasks:
            continue
        _start_task(row["uid"], rid, context)
    logger.info("Recovered %d active OTP task(s) on startup", len(active))


# ════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════

def main():
    if not BOT_TOKEN:
        print("ERROR: Set BOT_TOKEN environment variable!")
        print("  export BOT_TOKEN='your_token_here'")
        return

    init_db()
    logger.info("Database initialized successfully")

    async def _cleanup(app):
        for rid in list(_active_tasks.keys()):
            _stop_task(rid)
        _active_tasks.clear()
        _active_stop.clear()
        _number_msg_id.clear()
        _tracked_msgs.clear()
        _user_states.clear()
        global _http_session
        if _http_session and not _http_session.closed:
            await _http_session.close()

    app = Application.builder().token(BOT_TOKEN).post_stop(_cleanup).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(cb_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    async def error_handler(update, context):
        logger.error("Update %s caused error: %s", update, context.error)

    app.add_error_handler(error_handler)

    app.job_queue.run_once(_recover_tasks, when=0)

    logger.info("VOLTX Bot is now running")

    app.run_polling(drop_pending_updates=True, bootstrap_retries=10)
    logger.info("VOLTX Bot has been stopped")


if __name__ == "__main__":
    main()
