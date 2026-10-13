import os
import sqlite3
import asyncio
import threading
import csv
import io
import random
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    WebAppInfo, BotCommand, BufferedInputFile
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties

# ============================================================
# CONFIG
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "1329")
MINI_APP_URL = os.getenv("MINI_APP_URL", "https://grammintora.space")
DB_PATH = os.getenv("DB_PATH", "bot.db")
PORT = int(os.getenv("PORT", "10000"))
MAIN_ADMIN_ID = os.getenv("MAIN_ADMIN_ID", "")

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is not set")

# ============================================================
# FAKE HTTP SERVER
# ============================================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Gram Bot is running")

    def log_message(self, format, *args):
        pass

def run_http_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    server.serve_forever()

# ============================================================
# DATABASE
# ============================================================
class DB:
    def __init__(self, path):
        self.path = path
        self.init()

    def conn(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    def init(self):
        with self.conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    joined_at TEXT,
                    last_seen TEXT,
                    is_banned INTEGER DEFAULT 0,
                    is_shadow_banned INTEGER DEFAULT 0,
                    is_admin INTEGER DEFAULT 0,
                    lang TEXT DEFAULT 'ru',
                    referrer_id INTEGER DEFAULT NULL,
                    ref_count INTEGER DEFAULT 0,
                    last_request_at TEXT DEFAULT NULL,
                    total_requests INTEGER DEFAULT 0,
                    total_approved INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    action TEXT,
                    detail TEXT,
                    ts TEXT
                );

                CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    status TEXT DEFAULT 'pending',
                    code TEXT,
                    amount INTEGER,
                    admin_id INTEGER,
                    created_at TEXT,
                    resolved_at TEXT
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );

                CREATE TABLE IF NOT EXISTS templates (
                    name TEXT PRIMARY KEY,
                    text TEXT,
                    created_at TEXT
                );

                CREATE TABLE IF NOT EXISTS scheduled (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT,
                    send_at TEXT,
                    filter_type TEXT DEFAULT 'all',
                    created_by INTEGER,
                    sent INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS ab_tests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT,
                    variant_a TEXT,
                    variant_b TEXT,
                    clicks_a INTEGER DEFAULT 0,
                    clicks_b INTEGER DEFAULT 0,
                    created_at TEXT
                );
            """)

            # default settings
            defaults = {
                'maintenance': '0',
                'maintenance_text': '🛠 Бот на техническом перерыве. Скоро вернёмся!',
                'btn_text': '🎁 Забрать промокод',
                'welcome_text': '👋 Привет! Добро пожаловать в <b>Gram</b>.\n\nЗабери свой промокод и активируй его в приложении.',
                'request_limit_hours': '12',
                'ref_bonus_gram': '5',
                'ref_enabled': '1'
            }
            for k, v in defaults.items():
                c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

    def upsert_user(self, user_id, username, first_name, last_name, referrer_id=None):
        now = datetime.utcnow().isoformat()
        with self.conn() as c:
            existing = c.execute("SELECT user_id, referrer_id FROM users WHERE user_id = ?", (user_id,)).fetchone()
            if existing:
                c.execute("""
                    UPDATE users SET username=?, first_name=?, last_name=?, last_seen=?
                    WHERE user_id=?
                """, (username or "", first_name or "", last_name or "", now, user_id))
            else:
                c.execute("""
                    INSERT INTO users (user_id, username, first_name, last_name, joined_at, last_seen, referrer_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (user_id, username or "", first_name or "", last_name or "", now, now, referrer_id))
                # increment referrer count
                if referrer_id:
                    c.execute("UPDATE users SET ref_count = ref_count + 1 WHERE user_id = ?", (referrer_id,))

    def get_user(self, user_id):
        with self.conn() as c:
            row = c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            return dict(row) if row else None

    def list_users(self, limit=10, offset=0, filter_type='all'):
        with self.conn() as c:
            q = "SELECT * FROM users"
            params = []
            if filter_type == 'banned':
                q += " WHERE is_banned = 1"
            elif filter_type == 'active':
                since = (datetime.utcnow() - timedelta(days=7)).isoformat()
                q += " WHERE last_seen >= ? AND is_banned = 0"
                params.append(since)
            elif filter_type == 'new':
                since = (datetime.utcnow() - timedelta(days=1)).isoformat()
                q += " WHERE joined_at >= ?"
                params.append(since)
            elif filter_type == 'refs':
                q += " WHERE ref_count > 0 ORDER BY ref_count DESC"
            elif filter_type != 'refs':
                q += " ORDER BY joined_at DESC"
            q += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            rows = c.execute(q, params).fetchall()
            return [dict(r) for r in rows]

    def count_users(self, filter_type='all'):
        with self.conn() as c:
            if filter_type == 'banned':
                return c.execute("SELECT COUNT(*) AS n FROM users WHERE is_banned = 1").fetchone()["n"]
            elif filter_type == 'active':
                since = (datetime.utcnow() - timedelta(days=7)).isoformat()
                return c.execute("SELECT COUNT(*) AS n FROM users WHERE last_seen >= ? AND is_banned = 0", (since,)).fetchone()["n"]
            return c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]

    def count_active_24h(self):
        since = (datetime.utcnow() - timedelta(hours=24)).isoformat()
        with self.conn() as c:
            return c.execute("SELECT COUNT(*) AS n FROM users WHERE last_seen >= ?", (since,)).fetchone()["n"]

    def set_ban(self, user_id, banned):
        with self.conn() as c:
            c.execute("UPDATE users SET is_banned = ? WHERE user_id = ?", (1 if banned else 0, user_id))

    def set_shadow_ban(self, user_id, banned):
        with self.conn() as c:
            c.execute("UPDATE users SET is_shadow_banned = ? WHERE user_id = ?", (1 if banned else 0, user_id))

    def set_admin(self, user_id, is_admin):
        with self.conn() as c:
            c.execute("UPDATE users SET is_admin = ? WHERE user_id = ?", (1 if is_admin else 0, user_id))

    def set_lang(self, user_id, lang):
        with self.conn() as c:
            c.execute("UPDATE users SET lang = ? WHERE user_id = ?", (lang, user_id))

    def all_admin_ids(self):
        with self.conn() as c:
            return [r["user_id"] for r in c.execute("SELECT user_id FROM users WHERE is_admin = 1").fetchall()]

    def all_user_ids(self, exclude_banned=False, filter_type='all'):
        with self.conn() as c:
            q = "SELECT user_id FROM users WHERE 1=1"
            params = []
            if exclude_banned:
                q += " AND is_banned = 0"
            if filter_type == 'active':
                since = (datetime.utcnow() - timedelta(days=7)).isoformat()
                q += " AND last_seen >= ? AND is_banned = 0"
                params.append(since)
            elif filter_type == 'new':
                since = (datetime.utcnow() - timedelta(days=1)).isoformat()
                q += " AND joined_at >= ?"
                params.append(since)
            elif filter_type == 'nopromo':
                q += " AND total_approved = 0 AND is_banned = 0"
            return [r["user_id"] for r in c.execute(q, params).fetchall()]

    def log(self, user_id, action, detail=""):
        with self.conn() as c:
            c.execute(
                "INSERT INTO logs (user_id, action, detail, ts) VALUES (?, ?, ?, ?)",
                (user_id, action, detail, datetime.utcnow().isoformat())
            )

    def recent_logs(self, limit=30):
        with self.conn() as c:
            rows = c.execute("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]

    def get_setting(self, key, default=None):
        with self.conn() as c:
            row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default

    def set_setting(self, key, value):
        with self.conn() as c:
            c.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value))
            )

    # --- requests ---
    def create_request(self, user_id):
        with self.conn() as c:
            c.execute("""
                INSERT INTO requests (user_id, status, created_at) VALUES (?, 'pending', ?)
            """, (user_id, datetime.utcnow().isoformat()))
            req_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.execute("""
                UPDATE users SET last_request_at = ?, total_requests = total_requests + 1 WHERE user_id = ?
            """, (datetime.utcnow().isoformat(), user_id))
            return req_id

    def get_request(self, req_id):
        with self.conn() as c:
            row = c.execute("SELECT * FROM requests WHERE id = ?", (req_id,)).fetchone()
            return dict(row) if row else None

    def resolve_request(self, req_id, status, admin_id, code=None, amount=None):
        with self.conn() as c:
            c.execute("""
                UPDATE requests SET status = ?, admin_id = ?, code = ?, amount = ?, resolved_at = ?
                WHERE id = ?
            """, (status, admin_id, code, amount, datetime.utcnow().isoformat(), req_id))
            if status == 'approved':
                req = c.execute("SELECT user_id FROM requests WHERE id = ?", (req_id,)).fetchone()
                if req:
                    c.execute("UPDATE users SET total_approved = total_approved + 1 WHERE user_id = ?", (req["user_id"],))

    def list_pending_requests(self):
        with self.conn() as c:
            rows = c.execute("SELECT * FROM requests WHERE status = 'pending' ORDER BY created_at DESC").fetchall()
            return [dict(r) for r in rows]

    def list_requests(self, limit=20, status=None):
        with self.conn() as c:
            if status:
                rows = c.execute("SELECT * FROM requests WHERE status = ? ORDER BY created_at DESC LIMIT ?", (status, limit)).fetchall()
            else:
                rows = c.execute("SELECT * FROM requests ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]

    def can_request(self, user_id):
        user = self.get_user(user_id)
        if not user or not user["last_request_at"]:
            return True
        limit_hours = int(self.get_setting("request_limit_hours", "12"))
        last = datetime.fromisoformat(user["last_request_at"])
        return datetime.utcnow() - last > timedelta(hours=limit_hours)

    # --- templates ---
    def save_template(self, name, text):
        with self.conn() as c:
            c.execute("""
                INSERT INTO templates (name, text, created_at) VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET text = excluded.text
            """, (name, text, datetime.utcnow().isoformat()))

    def get_template(self, name):
        with self.conn() as c:
            row = c.execute("SELECT text FROM templates WHERE name = ?", (name,)).fetchone()
            return row["text"] if row else None

    def list_templates(self):
        with self.conn() as c:
            rows = c.execute("SELECT * FROM templates ORDER BY name").fetchall()
            return [dict(r) for r in rows]

    def delete_template(self, name):
        with self.conn() as c:
            c.execute("DELETE FROM templates WHERE name = ?", (name,))

    # --- scheduled ---
    def schedule_msg(self, text, send_at, filter_type, created_by):
        with self.conn() as c:
            c.execute("""
                INSERT INTO scheduled (text, send_at, filter_type, created_by) VALUES (?, ?, ?, ?)
            """, (text, send_at, filter_type, created_by))

    def due_scheduled(self):
        now = datetime.utcnow().isoformat()
        with self.conn() as c:
            rows = c.execute("SELECT * FROM scheduled WHERE send_at <= ? AND sent = 0", (now,)).fetchall()
            return [dict(r) for r in rows]

    def mark_sent(self, sid):
        with self.conn() as c:
            c.execute("UPDATE scheduled SET sent = 1 WHERE id = ?", (sid,))

db = DB(DB_PATH)

# ============================================================
# BOT
# ============================================================
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

# ============================================================
# STATES
# ============================================================
class AdminStates(StatesGroup):
    waiting_password = State()
    waiting_broadcast = State()
    waiting_broadcast_one = State()
    waiting_maintenance_text = State()
    waiting_btn_text = State()
    waiting_req_code = State()
    waiting_req_amount = State()
    waiting_template_name = State()
    waiting_template_text = State()
    waiting_schedule_text = State()
    waiting_schedule_time = State()
    waiting_schedule_filter = State()
    waiting_ab_variant_a = State()
    waiting_ab_variant_b = State()

# ============================================================
# HELPERS
# ============================================================
def is_maintenance():
    return db.get_setting("maintenance", "0") == "1"

def maintenance_text():
    return db.get_setting("maintenance_text", "🛠 Бот на техническом перерыве.")

def btn_text():
    return db.get_setting("btn_text", "🎁 Забрать промокод")

def welcome_text():
    return db.get_setting("welcome_text", "👋 Привет!")

def is_admin(user_id):
    u = db.get_user(user_id)
    return bool(u and u["is_admin"])

def admin_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm_stats")],
        [InlineKeyboardButton(text="🎫 Заявки", callback_data="adm_requests")],
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="adm_users_0")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="adm_broadcast")],
        [InlineKeyboardButton(text="🎯 Рефералы", callback_data="adm_refs")],
        [InlineKeyboardButton(text="💬 Шаблоны", callback_data="adm_templates")],
        [InlineKeyboardButton(text="📅 Запланированные", callback_data="adm_sched")],
        [InlineKeyboardButton(text="📈 A/B тесты", callback_data="adm_ab")],
        [InlineKeyboardButton(text="📥 Экспорт", callback_data="adm_export")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="adm_settings")],
        [InlineKeyboardButton(text="🛠 Тех-перерыв", callback_data="adm_maint")],
        [InlineKeyboardButton(text="📝 Логи", callback_data="adm_logs")],
        [InlineKeyboardButton(text="🚪 Выйти", callback_data="adm_logout")],
    ])

def user_menu():
    if is_maintenance() and not is_admin(0):
        pass
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=btn_text(), callback_data="user_request")],
        [InlineKeyboardButton(text="💎 Открыть Gram", web_app=WebAppInfo(url=MINI_APP_URL))],
        [InlineKeyboardButton(text="🏆 Топ рефералов", callback_data="user_top_refs")],
    ])
    return kb

def user_menu_for(user_id):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=btn_text(), callback_data="user_request")],
        [InlineKeyboardButton(text="💎 Открыть Gram", web_app=WebAppInfo(url=MINI_APP_URL))],
        [InlineKeyboardButton(text="🔗 Моя ссылка", callback_data="user_my_ref")],
        [InlineKeyboardButton(text="🏆 Топ", callback_data="user_top_refs")],
    ])
    return kb

def is_maintenance_blocked(user_id):
    return is_maintenance() and not is_admin(user_id)

# ============================================================
# USER HANDLERS
# ============================================================
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    u = message.from_user
    args = message.text.split(maxsplit=1)[1] if len(message.text.split()) > 1 else ""

    referrer_id = None
    if args.startswith("ref_"):
        try:
            referrer_id = int(args[4:])
            if referrer_id == u.id:
                referrer_id = None
        except Exception:
            referrer_id = None

    db.upsert_user(u.id, u.username, u.first_name, u.last_name, referrer_id)
    db.log(u.id, "start", f"ref={referrer_id}" if referrer_id else "")

    user = db.get_user(u.id)
    if user and user["is_banned"]:
        return

    if is_maintenance_blocked(u.id):
        await message.answer(maintenance_text())
        return

    await message.answer(
        welcome_text(),
        reply_markup=user_menu_for(u.id)
    )

@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    u = message.from_user
    db.upsert_user(u.id, u.username, u.first_name, u.last_name)

    if is_admin(u.id):
        await message.answer("🔐 <b>Админ-панель</b>", reply_markup=admin_menu())
        return

    await message.answer("🔐 Введите пароль:")
    await state.set_state(AdminStates.waiting_password)
    await state.update_data(candidate_id=u.id)

@router.message(AdminStates.waiting_password)
async def check_password(message: Message, state: FSMContext):
    data = await state.get_data()
    candidate_id = data.get("candidate_id")
    if message.from_user.id != candidate_id:
        return

    try:
        await message.delete()
    except Exception:
        pass

    if (message.text or "").strip() == ADMIN_PASSWORD:
        db.set_admin(message.from_user.id, 1)
        db.log(message.from_user.id, "admin_login", "")
        await message.answer("✅ Доступ разрешён.\n\n🔐 <b>Админ-панель</b>", reply_markup=admin_menu())
    else:
        db.log(message.from_user.id, "admin_wrong_pass", "")
        await message.answer("❌ Неверный пароль.")
    await state.clear()

# ============================================================
# USER: REQUEST PROMO
# ============================================================
@router.callback_query(F.data == "user_request")
async def user_request(call: CallbackQuery, state: FSMContext):
    u = call.from_user
    db.upsert_user(u.id, u.username, u.first_name, u.last_name)

    user = db.get_user(u.id)
    if user and user["is_banned"]:
        await call.answer("Доступ ограничен", show_alert=True)
        return

    if is_maintenance_blocked(u.id):
        await call.answer("Бот на перерыве", show_alert=True)
        return

    if user and user["is_shadow_banned"]:
        await call.answer("Заявка принята", show_alert=False)
        await call.message.answer("⏳ Заявка отправлена. Ожидайте.")
        return

    if not db.can_request(u.id):
        limit = db.get_setting("request_limit_hours", "12")
        last = user["last_request_at"]
        remain = "—"
        if last:
            try:
                t = datetime.fromisoformat(last) + timedelta(hours=int(limit))
                delta = t - datetime.utcnow()
                h = delta.seconds // 3600
                m = (delta.seconds % 3600) // 60
                remain = f"{h}ч {m}м"
            except Exception:
                remain = "—"
        await call.answer(f"Следующая заявка через {remain}", show_alert=True)
        return

    req_id = db.create_request(u.id)
    db.log(u.id, "request_created", f"id={req_id}")

    # Notify all admins
    username = f"@{u.username}" if u.username else "—"
    name = (u.first_name or "") + " " + (u.last_name or "")
    name = name.strip() or "—"

    text = (
        f"🎁 <b>Новая заявка</b> #{req_id}\n\n"
        f"👤 <b>{name}</b>\n"
        f"🔗 {username}\n"
        f"🆔 <code>{u.id}</code>\n"
        f"🕐 {datetime.utcnow().strftime('%d.%m.%Y %H:%M')} UTC"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"adm_req_ok_{req_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_req_no_{req_id}"),
        ],
        [InlineKeyboardButton(text="✉️ Написать", callback_data=f"adm_req_dm_{req_id}")],
    ])

    for admin_id in db.all_admin_ids():
        try:
            await bot.send_message(admin_id, text, reply_markup=kb)
        except Exception:
            pass

    await call.answer("Заявка отправлена администрации", show_alert=False)
    await call.message.answer(
        f"⏳ Ваша заявка принята.\n\n"
        f"Администратор рассмотрит её в ближайшее время. "
        f"Ожидайте — код придёт в этот чат.",
    )

@router.callback_query(F.data == "user_my_ref")
async def user_my_ref(call: CallbackQuery):
    u = call.from_user
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{u.id}"

    user = db.get_user(u.id)
    count = user["ref_count"] if user else 0
    bonus = db.get_setting("ref_bonus_gram", "5")

    await call.message.answer(
        f"🔗 <b>Ваша реферальная ссылка:</b>\n\n"
        f"<code>{link}</code>\n\n"
        f"👥 Приглашено: <b>{count}</b>\n"
        f"🎁 За каждого друга: <b>+{bonus} GRAM</b> к вашему промокоду\n\n"
        f"Копируйте и приглашайте!"
    )
    await call.answer()

@router.callback_query(F.data == "user_top_refs")
async def user_top_refs(call: CallbackQuery):
    with db.conn() as c:
        rows = c.execute(
            "SELECT first_name, username, ref_count FROM users WHERE ref_count > 0 ORDER BY ref_count DESC LIMIT 10"
        ).fetchall()

    if not rows:
        await call.answer("Пока никого", show_alert=True)
        return

    text = "🏆 <b>Топ рефералов</b>\n\n"
    for i, row in enumerate(rows, 1):
        name = row["first_name"] or "—"
        username = f"@{row['username']}" if row["username"] else ""
        text += f"{i}. <b>{name}</b> {username} — {row['ref_count']} 👥\n"

    await call.message.answer(text)
    await call.answer()

# ============================================================
# ADMIN: REQUESTS
# ============================================================
@router.callback_query(F.data.startswith("adm_req_ok_"))
async def adm_req_ok(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа")
        return

    req_id = int(call.data.split("_")[-1])
    req = db.get_request(req_id)
    if not req or req["status"] != "pending":
        await call.answer("Заявка уже обработана", show_alert=True)
        return

    await state.update_data(req_id=req_id)
    await state.set_state(AdminStates.waiting_req_code)
    await call.message.edit_text(
        f"✅ Одобрение заявки #{req_id}\n\n"
        f"Введите <b>текст промокода</b> (например A7K2M9P4X8Q3):"
    )
    await call.answer()

@router.message(AdminStates.waiting_req_code)
async def adm_req_code_step(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    code = (message.text or "").strip().upper()
    if len(code) < 3 or len(code) > 32:
        await message.answer("❌ Код от 3 до 32 символов. Попробуйте снова:")
        return

    await state.update_data(req_code=code)
    await state.set_state(AdminStates.waiting_req_amount)
    await message.answer(f"Код: <code>{code}</code>\n\n💰 Введите <b>сумму GRAM</b> (целое число):")

@router.message(AdminStates.waiting_req_amount)
async def adm_req_amount_step(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    try:
        amount = int((message.text or "").strip())
        if amount <= 0 or amount > 100000:
            raise ValueError()
    except Exception:
        await message.answer("❌ Введите целое число 1-100000:")
        return

    data = await state.get_data()
    req_id = data.get("req_id")
    code = data.get("req_code")

    req = db.get_request(req_id)
    if not req:
        await message.answer("❌ Заявка не найдена")
        await state.clear()
        return

    db.resolve_request(req_id, 'approved', message.from_user.id, code, amount)
    db.log(message.from_user.id, "req_approved", f"#{req_id} user={req['user_id']} code={code} amount={amount}")

    # Send to user with link
    app_link = f"{MINI_APP_URL}?code={code}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Открыть Gram", web_app=WebAppInfo(url=app_link))],
    ])

    try:
        await bot.send_message(
            req["user_id"],
            f"🎉 <b>Ваша заявка одобрена!</b>\n\n"
            f"🎟 Промокод: <code>{code}</code>\n"
            f"💰 Сумма: <b>{amount} GRAM</b>\n\n"
            f"Нажмите на кнопку ниже — код подставится автоматически:",
            reply_markup=kb
        )
        await message.answer(f"✅ Промокод отправлен юзеру <code>{req['user_id']}</code>")
    except Exception as e:
        await message.answer(f"❌ Не удалось отправить: {e}")

    await state.clear()

@router.callback_query(F.data.startswith("adm_req_no_"))
async def adm_req_no(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return

    req_id = int(call.data.split("_")[-1])
    req = db.get_request(req_id)
    if not req or req["status"] != "pending":
        await call.answer("Уже обработана", show_alert=True)
        return

    db.resolve_request(req_id, 'rejected', call.from_user.id)
    db.log(call.from_user.id, "req_rejected", f"#{req_id} user={req['user_id']}")

    try:
        await bot.send_message(req["user_id"], "❌ К сожалению, ваша заявка отклонена.")
    except Exception:
        pass

    await call.message.edit_text(f"❌ Заявка #{req_id} отклонена")
    await call.answer()

@router.callback_query(F.data.startswith("adm_req_dm_"))
async def adm_req_dm(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return

    req_id = int(call.data.split("_")[-1])
    req = db.get_request(req_id)
    if not req:
        return

    await state.update_data(dm_user_id=req["user_id"])
    await state.set_state(AdminStates.waiting_broadcast_one)
    await call.message.edit_text(f"✉️ Введите сообщение для <code>{req['user_id']}</code>:")
    await call.answer()

@router.callback_query(F.data == "adm_requests")
async def adm_requests(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return

    pending = db.list_pending_requests()
    text = f"🎫 <b>Заявки</b>\n\n"
    if not pending:
        text += "<i>Нет ожидающих заявок</i>"
    else:
        text += f"Ожидают: <b>{len(pending)}</b>"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏳ Ожидающие", callback_data="adm_reqs_pending")],
        [InlineKeyboardButton(text="✅ Одобренные", callback_data="adm_reqs_approved")],
        [InlineKeyboardButton(text="❌ Отклонённые", callback_data="adm_reqs_rejected")],
        [InlineKeyboardButton(text="← Назад", callback_data="adm_back")],
    ])
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("adm_reqs_"))
async def adm_reqs_list(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return

    status = call.data.split("_")[-1]
    status_map = {'pending': 'pending', 'approved': 'approved', 'rejected': 'rejected'}
    status = status_map.get(status, 'pending')

    reqs = db.list_requests(20, status)
    if not reqs:
        await call.answer("Пусто", show_alert=True)
        return

    text = f"🎫 <b>{status.capitalize()}</b> — {len(reqs)} заявок\n\n"

    kb_rows = []
    for r in reqs[:15]:
        user = db.get_user(r["user_id"])
        name = (user["first_name"] if user else "?") or "?"
        name = name[:15]
        if r["status"] == 'approved':
            kb_rows.append([InlineKeyboardButton(
                text=f"✅ #{r['id']} {name} · {r['amount']} GRAM",
                callback_data=f"adm_reqview_{r['id']}"
            )])
        elif r["status"] == 'pending':
            kb_rows.append([InlineKeyboardButton(
                text=f"⏳ #{r['id']} {name}",
                callback_data=f"adm_reqview_{r['id']}"
            )])
        else:
            kb_rows.append([InlineKeyboardButton(
                text=f"❌ #{r['id']} {name}",
                callback_data=f"adm_reqview_{r['id']}"
            )])

    kb_rows.append([InlineKeyboardButton(text="← Назад", callback_data="adm_requests")])

    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    await call.answer()

@router.callback_query(F.data.startswith("adm_reqview_"))
async def adm_reqview(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return

    req_id = int(call.data.split("_")[-1])
    req = db.get_request(req_id)
    if not req:
        return

    user = db.get_user(req["user_id"])
    name = f"{user['first_name'] or ''} {user['last_name'] or ''}".strip() or "—"
    username = f"@{user['username']}" if user and user['username'] else "—"

    text = (
        f"🎫 <b>Заявка #{req['id']}</b>\n\n"
        f"👤 {name}\n"
        f"🔗 {username}\n"
        f"🆔 <code>{req['user_id']}</code>\n"
        f"📌 Статус: <b>{req['status']}</b>\n"
    )
    if req['code']:
        text += f"🎟 Код: <code>{req['code']}</code>\n"
    if req['amount']:
        text += f"💰 Сумма: <b>{req['amount']} GRAM</b>\n"
    text += f"🕐 Создана: {req['created_at'][:16].replace('T', ' ')}\n"

    kb_rows = []
    if req['status'] == 'pending':
        kb_rows.append([
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"adm_req_ok_{req_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_req_no_{req_id}"),
        ])
    kb_rows.append([InlineKeyboardButton(text="✉️ Написать юзеру", callback_data=f"adm_req_dm_{req_id}")])
    kb_rows.append([InlineKeyboardButton(text="← Назад", callback_data="adm_requests")])

    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    await call.answer()

# ============================================================
# ADMIN: STATS
# ============================================================
@router.callback_query(F.data == "adm_stats")
async def adm_stats(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return

    total = db.count_users()
    active_24h = db.count_active_24h()
    active_7d = db.count_users('active')
    banned = db.count_users('banned')

    with db.conn() as c:
        reqs_total = c.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        reqs_ok = c.execute("SELECT COUNT(*) FROM requests WHERE status = 'approved'").fetchone()[0]
        reqs_pending = c.execute("SELECT COUNT(*) FROM requests WHERE status = 'pending'").fetchone()[0]
        refs_total = c.execute("SELECT SUM(ref_count) FROM users").fetchone()[0] or 0

    funnel = f"{(reqs_ok / total * 100):.1f}%" if total else "0%"
    maint = "🟢 Включён" if is_maintenance() else "🔴 Выключен"

    txt = (
        f"📊 <b>Статистика</b>\n\n"
        f"👥 Всего: <b>{total}</b>\n"
        f"🔥 Активных 24ч: <b>{active_24h}</b>\n"
        f"📅 Активных 7д: <b>{active_7d}</b>\n"
        f"🚫 Забанено: <b>{banned}</b>\n\n"
        f"🎫 <b>Заявки</b>\n"
        f"Всего: <b>{reqs_total}</b>\n"
        f"Одобрено: <b>{reqs_ok}</b>\n"
        f"Ожидают: <b>{reqs_pending}</b>\n"
        f"Воронка: <b>{funnel}</b>\n\n"
        f"🔗 Рефералов: <b>{refs_total}</b>\n"
        f"🛠 Тех-перерыв: {maint}"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📈 Активность по часам", callback_data="adm_stats_hours")],
        [InlineKeyboardButton(text="← Назад", callback_data="adm_back")],
    ])
    await call.message.edit_text(txt, reply_markup=kb)
    await call.answer()

@router.callback_query(F.data == "adm_stats_hours")
async def adm_stats_hours(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return

    now = datetime.utcnow()
    buckets = {}
    for i in range(24):
        buckets[i] = 0

    with db.conn() as c:
        since = (now - timedelta(hours=24)).isoformat()
        rows = c.execute("SELECT last_seen FROM users WHERE last_seen >= ?", (since,)).fetchall()
        for r in rows:
            try:
                dt = datetime.fromisoformat(r["last_seen"])
                h = dt.hour
                buckets[h] += 1
            except Exception:
                pass

    max_v = max(buckets.values()) or 1
    lines = ["📈 <b>Активность за 24ч</b>\n"]
    for h in range(24):
        v = buckets[h]
        bar = "█" * int(v / max_v * 20)
        lines.append(f"{h:02d}:00 <code>{bar}</code> {v}")

    await call.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← Назад", callback_data="adm_stats")],
    ]))
    await call.answer()

# ============================================================
# ADMIN: USERS
# ============================================================
@router.callback_query(F.data.startswith("adm_users_"))
async def adm_users(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return

    parts = call.data.split("_")
    page = int(parts[-1])
    filter_type = 'all'
    if len(parts) > 3 and parts[2] in ('active', 'banned', 'new', 'refs'):
        filter_type = parts[2]
        page = int(parts[-1])

    users = db.list_users(limit=10, offset=page * 10, filter_type=filter_type)
    total = db.count_users(filter_type)

    if not users:
        await call.answer("Пусто", show_alert=True)
        return

    buttons = []
    for u in users:
        name = u["first_name"] or "—"
        status = "🚫" if u["is_banned"] else ("👑" if u["is_admin"] else "✅")
        refs = f" · {u['ref_count']}r" if u["ref_count"] else ""
        buttons.append([InlineKeyboardButton(
            text=f"{status} {name[:18]}{refs}",
            callback_data=f"adm_user_{u['user_id']}"
        )])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="←", callback_data=f"adm_users_{filter_type}_{page-1}" if filter_type != 'all' else f"adm_users_{page-1}"))
    if (page + 1) * 10 < total:
        nav.append(InlineKeyboardButton(text="→", callback_data=f"adm_users_{filter_type}_{page+1}" if filter_type != 'all' else f"adm_users_{page+1}"))
    if nav:
        buttons.append(nav)

    # filter row
    buttons.append([
        InlineKeyboardButton(text="📋 Все", callback_data="adm_users_0"),
        InlineKeyboardButton(text="🔥 Активные", callback_data="adm_users_active_0"),
        InlineKeyboardButton(text="🆕 Новые", callback_data="adm_users_new_0"),
    ])
    buttons.append([
        InlineKeyboardButton(text="🚫 Бан", callback_data="adm_users_banned_0"),
        InlineKeyboardButton(text="🔗 Рефоводы", callback_data="adm_users_refs_0"),
    ])
    buttons.append([InlineKeyboardButton(text="← Назад", callback_data="adm_back")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    filter_label = {"all": "Все", "active": "Активные", "banned": "Забаненные", "new": "Новые", "refs": "Рефоводы"}.get(filter_type, "Все")
    await call.message.edit_text(f"👥 <b>Пользователи</b> — {filter_label} ({page*10+1}-{page*10+len(users)} из {total})", reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("adm_user_"))
async def adm_user(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return

    uid = int(call.data.split("_")[-1])
    u = db.get_user(uid)
    if not u:
        await call.answer("Не найден", show_alert=True)
        return

    name = f"{u['first_name'] or ''} {u['last_name'] or ''}".strip() or "—"
    username = f"@{u['username']}" if u["username"] else "—"
    status = "🚫 Забанен" if u["is_banned"] else ("👑 Админ" if u["is_admin"] else "✅ Активен")
    if u["is_shadow_banned"]:
        status += " · 🌑 Тень"
    joined = u["joined_at"][:16].replace("T", " ") if u["joined_at"] else "—"
    seen = u["last_seen"][:16].replace("T", " ") if u["last_seen"] else "—"

    txt = (
        f"👤 <b>{name}</b>\n"
        f"🔗 {username}\n"
        f"🆔 <code>{uid}</code>\n"
        f"📅 Зашёл: {joined}\n"
        f"👀 Последний раз: {seen}\n"
        f"📌 Статус: {status}\n\n"
        f"🎫 Заявок: <b>{u['total_requests']}</b> · Одобрено: <b>{u['total_approved']}</b>\n"
        f"🔗 Рефералов: <b>{u['ref_count']}</b>\n"
        f"🌐 Язык: <b>{u['lang'] or 'ru'}</b>"
    )

    btns = []
    if u["is_banned"]:
        btns.append([InlineKeyboardButton(text="✅ Разбанить", callback_data=f"adm_unban_{uid}")])
    else:
        btns.append([InlineKeyboardButton(text="🚫 Забанить", callback_data=f"adm_ban_{uid}")])

    if u["is_shadow_banned"]:
        btns.append([InlineKeyboardButton(text="🌑 Убрать теневой бан", callback_data=f"adm_unshb_{uid}")])
    else:
        btns.append([InlineKeyboardButton(text="🌑 Теневой бан", callback_data=f"adm_shb_{uid}")])

    btns.append([InlineKeyboardButton(text="✉️ Написать", callback_data=f"adm_dm_{uid}")])
    if not u["is_admin"]:
        btns.append([InlineKeyboardButton(text="👑 Сделать админом", callback_data=f"adm_mkadmin_{uid}")])
    else:
        btns.append([InlineKeyboardButton(text="❌ Снять админа", callback_data=f"adm_rmadmin_{uid}")])
    btns.append([InlineKeyboardButton(text="🌐 Сменить язык", callback_data=f"adm_lang_{uid}")])
    btns.append([InlineKeyboardButton(text="← Назад", callback_data="adm_users_0")])

    await call.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
    await call.answer()

@router.callback_query(F.data.startswith("adm_ban_"))
async def adm_ban(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    uid = int(call.data.split("_")[-1])
    db.set_ban(uid, True)
    db.log(call.from_user.id, "ban", str(uid))
    await call.answer("Забанен")
    await adm_user(call)

@router.callback_query(F.data.startswith("adm_unban_"))
async def adm_unban(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    uid = int(call.data.split("_")[-1])
    db.set_ban(uid, False)
    db.log(call.from_user.id, "unban", str(uid))
    await call.answer("Разбанен")
    await adm_user(call)

@router.callback_query(F.data.startswith("adm_shb_"))
async def adm_shb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    uid = int(call.data.split("_")[-1])
    db.set_shadow_ban(uid, True)
    db.log(call.from_user.id, "shadow_ban", str(uid))
    await call.answer("Теневой бан")
    await adm_user(call)

@router.callback_query(F.data.startswith("adm_unshb_"))
async def adm_unshb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    uid = int(call.data.split("_")[-1])
    db.set_shadow_ban(uid, False)
    db.log(call.from_user.id, "shadow_unban", str(uid))
    await call.answer("Снято")
    await adm_user(call)

@router.callback_query(F.data.startswith("adm_mkadmin_"))
async def adm_mkadmin(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    uid = int(call.data.split("_")[-1])
    db.set_admin(uid, 1)
    db.log(call.from_user.id, "make_admin", str(uid))
    await call.answer("Назначен")
    await adm_user(call)

@router.callback_query(F.data.startswith("adm_rmadmin_"))
async def adm_rmadmin(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    uid = int(call.data.split("_")[-1])
    db.set_admin(uid, 0)
    db.log(call.from_user.id, "rm_admin", str(uid))
    await call.answer("Снят")
    await adm_user(call)

@router.callback_query(F.data.startswith("adm_lang_"))
async def adm_lang(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    uid = int(call.data.split("_")[-1])
    u = db.get_user(uid)
    new_lang = 'en' if (u['lang'] or 'ru') == 'ru' else 'ru'
    db.set_lang(uid, new_lang)
    await call.answer(f"Язык: {new_lang}")
    await adm_user(call)

@router.callback_query(F.data.startswith("adm_dm_"))
async def adm_dm(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id): return
    uid = int(call.data.split("_")[-1])
    await state.update_data(dm_user_id=uid)
    await state.set_state(AdminStates.waiting_broadcast_one)
    await call.message.edit_text(f"✉️ Введите сообщение для <code>{uid}</code>:")
    await call.answer()

@router.message(AdminStates.waiting_broadcast_one)
async def send_dm(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    data = await state.get_data()
    uid = data.get("dm_user_id")
    try:
        await bot.send_message(uid, f"📩 <b>Сообщение от администрации:</b>\n\n{message.text}")
        await message.answer("✅ Отправлено.")
        db.log(message.from_user.id, "dm_sent", str(uid))
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
    await state.clear()

# ============================================================
# ADMIN: BROADCAST
# ============================================================
@router.callback_query(F.data == "adm_broadcast")
async def adm_broadcast(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id): return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Всем", callback_data="adm_bc_all")],
        [InlineKeyboardButton(text="🔥 Активным (7д)", callback_data="adm_bc_active")],
        [InlineKeyboardButton(text="🆕 Новым (24ч)", callback_data="adm_bc_new")],
        [InlineKeyboardButton(text="🎫 Без промокода", callback_data="adm_bc_nopromo")],
        [InlineKeyboardButton(text="← Назад", callback_data="adm_back")],
    ])
    await call.message.edit_text("📢 <b>Рассылка</b>\n\nВыберите получателей:", reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("adm_bc_"))
async def adm_bc_target(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id): return
    target = call.data.split("_")[-1]
    await state.update_data(bc_target=target)
    await state.set_state(AdminStates.waiting_broadcast)
    await call.message.edit_text(f"📢 Цель: <b>{target}</b>\n\nВведите текст рассылки:")
    await call.answer()

@router.message(AdminStates.waiting_broadcast)
async def adm_bc_send(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    data = await state.get_data()
    target = data.get("bc_target", "all")

    filter_map = {
        'all': 'all', 'active': 'active', 'new': 'new', 'nopromo': 'nopromo'
    }
    ids = db.all_user_ids(exclude_banned=True, filter_type=filter_map.get(target, 'all'))

    sent = 0
    failed = 0
    for uid in ids:
        try:
            await bot.send_message(uid, f"📢 <b>Объявление:</b>\n\n{message.text}")
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1

    db.log(message.from_user.id, "broadcast", f"target={target} sent={sent} failed={failed}")
    await message.answer(f"✅ Отправлено: <b>{sent}</b>\n❌ Ошибок: <b>{failed}</b>")
    await state.clear()

# ============================================================
# ADMIN: REFS
# ============================================================
@router.callback_query(F.data == "adm_refs")
async def adm_refs(call: CallbackQuery):
    if not is_admin(call.from_user.id): return

    with db.conn() as c:
        rows = c.execute("""
            SELECT user_id, first_name, username, ref_count
            FROM users WHERE ref_count > 0
            ORDER BY ref_count DESC LIMIT 20
        """).fetchall()

    total = sum(r["ref_count"] for r in rows) if rows else 0
    txt = f"🔗 <b>Топ рефералов</b>\n\nВсего: <b>{total}</b>\n\n"
    for i, r in enumerate(rows, 1):
        name = r["first_name"] or "—"
        txt += f"{i}. {name} — <b>{r['ref_count']}</b>\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← Назад", callback_data="adm_back")],
    ])
    await call.message.edit_text(txt, reply_markup=kb)
    await call.answer()

# ============================================================
# ADMIN: TEMPLATES
# ============================================================
@router.callback_query(F.data == "adm_templates")
async def adm_templates(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    templates = db.list_templates()
    txt = "💬 <b>Шаблоны сообщений</b>\n\n"
    if not templates:
        txt += "<i>Пусто. Создайте первый.</i>"
    else:
        for t in templates:
            txt += f"• <code>{t['name']}</code>\n"

    buttons = []
    for t in templates[:10]:
        buttons.append([InlineKeyboardButton(text=f"🗑 {t['name']}", callback_data=f"adm_tpl_del_{t['name']}")])

    buttons.append([InlineKeyboardButton(text="➕ Создать шаблон", callback_data="adm_tpl_new")])
    buttons.append([InlineKeyboardButton(text="← Назад", callback_data="adm_back")])

    await call.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await call.answer()

@router.callback_query(F.data == "adm_tpl_new")
async def adm_tpl_new(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id): return
    await state.set_state(AdminStates.waiting_template_name)
    await call.message.edit_text("💬 Введите <b>имя шаблона</b> (английскими, без пробелов):")
    await call.answer()

@router.message(AdminStates.waiting_template_name)
async def adm_tpl_name(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    name = (message.text or "").strip().lower().replace(" ", "_")
    if len(name) < 2 or len(name) > 32:
        await message.answer("❌ Имя 2-32 символа. Снова:")
        return
    await state.update_data(tpl_name=name)
    await state.set_state(AdminStates.waiting_template_text)
    await message.answer(f"Имя: <code>{name}</code>\n\nВведите текст шаблона:")

@router.message(AdminStates.waiting_template_text)
async def adm_tpl_text(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    data = await state.get_data()
    name = data.get("tpl_name")
    text = message.text or ""
    db.save_template(name, text)
    await message.answer(f"✅ Шаблон <code>{name}</code> сохранён.")
    await state.clear()

@router.callback_query(F.data.startswith("adm_tpl_del_"))
async def adm_tpl_del(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    name = call.data.replace("adm_tpl_del_", "")
    db.delete_template(name)
    await call.answer(f"Удалён: {name}")
    await adm_templates(call)

# ============================================================
# ADMIN: SCHEDULED
# ============================================================
@router.callback_query(F.data == "adm_sched")
async def adm_sched(call: CallbackQuery):
    if not is_admin(call.from_user.id): return

    with db.conn() as c:
        rows = c.execute("SELECT * FROM scheduled WHERE sent = 0 ORDER BY send_at").fetchall()

    txt = "📅 <b>Запланированные</b>\n\n"
    if not rows:
        txt += "<i>Пусто</i>"
    else:
        for r in rows:
            txt += f"• #{r['id']} — {r['send_at'][:16]} — {r['filter_type']}\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать", callback_data="adm_sched_new")],
        [InlineKeyboardButton(text="← Назад", callback_data="adm_back")],
    ])
    await call.message.edit_text(txt, reply_markup=kb)
    await call.answer()

@router.callback_query(F.data == "adm_sched_new")
async def adm_sched_new(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id): return
    await state.set_state(AdminStates.waiting_schedule_text)
    await call.message.edit_text("📅 Введите <b>текст сообщения</b>:")
    await call.answer()

@router.message(AdminStates.waiting_schedule_text)
async def adm_sched_text(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    await state.update_data(sched_text=message.text)
    await state.set_state(AdminStates.waiting_schedule_time)
    await message.answer(
        "Введите <b>время UTC</b> в формате:\n"
        "<code>YYYY-MM-DD HH:MM</code>\n\n"
        "Например: <code>2026-10-15 18:30</code>"
    )

@router.message(AdminStates.waiting_schedule_time)
async def adm_sched_time(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    try:
        dt = datetime.strptime((message.text or "").strip(), "%Y-%m-%d %H:%M")
    except Exception:
        await message.answer("❌ Формат: YYYY-MM-DD HH:MM")
        return
    await state.update_data(sched_time=dt.isoformat())
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Всем", callback_data="adm_schf_all")],
        [InlineKeyboardButton(text="🔥 Активным", callback_data="adm_schf_active")],
        [InlineKeyboardButton(text="🆕 Новым", callback_data="adm_schf_new")],
        [InlineKeyboardButton(text="🎫 Без промо", callback_data="adm_schf_nopromo")],
    ])
    await state.set_state(AdminStates.waiting_schedule_filter)
    await message.answer("Выберите получателей:", reply_markup=kb)

@router.callback_query(F.data.startswith("adm_schf_"))
async def adm_schf(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id): return
    target = call.data.split("_")[-1]
    data = await state.get_data()
    text = data.get("sched_text")
    send_at = data.get("sched_time")
    db.schedule_msg(text, send_at, target, call.from_user.id)
    await call.message.edit_text(f"✅ Запланировано на {send_at[:16]} (UTC) → {target}")
    await state.clear()
    await call.answer()

async def scheduler_loop():
    while True:
        try:
            due = db.due_scheduled()
            for s in due:
                ids = db.all_user_ids(exclude_banned=True, filter_type=s["filter_type"])
                for uid in ids:
                    try:
                        await bot.send_message(uid, f"📢 <b>Объявление:</b>\n\n{s['text']}")
                        await asyncio.sleep(0.05)
                    except Exception:
                        pass
                db.mark_sent(s["id"])
        except Exception:
            pass
        await asyncio.sleep(30)

# ============================================================
# ADMIN: A/B TESTS
# ============================================================
@router.callback_query(F.data == "adm_ab")
async def adm_ab(call: CallbackQuery):
    if not is_admin(call.from_user.id): return

    with db.conn() as c:
        rows = c.execute("SELECT * FROM ab_tests ORDER BY id DESC LIMIT 10").fetchall()

    txt = "📈 <b>A/B тесты кнопки</b>\n\n"
    if not rows:
        txt += "<i>Пусто</i>"
    else:
        for r in rows:
            total = r["clicks_a"] + r["clicks_b"]
            pct_a = (r["clicks_a"] / total * 100) if total else 0
            pct_b = (r["clicks_b"] / total * 100) if total else 0
            txt += f"• <b>{r['name']}</b>\n  A: {r['clicks_a']} ({pct_a:.0f}%)\n  B: {r['clicks_b']} ({pct_b:.0f}%)\n\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать тест", callback_data="adm_ab_new")],
        [InlineKeyboardButton(text="← Назад", callback_data="adm_back")],
    ])
    await call.message.edit_text(txt, reply_markup=kb)
    await call.answer()

@router.callback_query(F.data == "adm_ab_new")
async def adm_ab_new(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id): return
    await state.set_state(AdminStates.waiting_ab_variant_a)
    await call.message.edit_text("📈 Введите <b>текст варианта A</b>:")
    await call.answer()

@router.message(AdminStates.waiting_ab_variant_a)
async def adm_ab_a(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    await state.update_data(ab_a=message.text)
    await state.set_state(AdminStates.waiting_ab_variant_b)
    await message.answer("📈 Введите <b>текст варианта B</b>:")

@router.message(AdminStates.waiting_ab_variant_b)
async def adm_ab_b(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    data = await state.get_data()
    a = data.get("ab_a")
    b = message.text
    with db.conn() as c:
        c.execute("""
            INSERT INTO ab_tests (name, variant_a, variant_b, created_at)
            VALUES (?, ?, ?, ?)
        """, (f"test_{int(datetime.utcnow().timestamp())}", a, b, datetime.utcnow().isoformat()))
    await message.answer("✅ A/B тест создан. Пока не активен — распределение кликов работает автоматически когда вы назначите тест активным.")
    await state.clear()

# ============================================================
# ADMIN: EXPORT
# ============================================================
@router.callback_query(F.data == "adm_export")
async def adm_export(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Пользователи (CSV)", callback_data="adm_exp_users")],
        [InlineKeyboardButton(text="🎫 Заявки (CSV)", callback_data="adm_exp_reqs")],
        [InlineKeyboardButton(text="📝 Логи (CSV)", callback_data="adm_exp_logs")],
        [InlineKeyboardButton(text="← Назад", callback_data="adm_back")],
    ])
    await call.message.edit_text("📥 <b>Экспорт</b>", reply_markup=kb)
    await call.answer()

@router.callback_query(F.data == "adm_exp_users")
async def adm_exp_users(call: CallbackQuery):
    if not is_admin(call.from_user.id): return

    with db.conn() as c:
        rows = c.execute("SELECT * FROM users").fetchall()

    buf = io.StringIO()
    buf.write("user_id,username,first_name,last_name,joined_at,last_seen,is_banned,is_admin,ref_count\n")
    for r in rows:
        buf.write(f"{r['user_id']},{r['username']},{r['first_name']},{r['last_name']},{r['joined_at']},{r['last_seen']},{r['is_banned']},{r['is_admin']},{r['ref_count']}\n")

    file = BufferedInputFile(buf.getvalue().encode(), filename=f"users_{int(datetime.utcnow().timestamp())}.csv")
    await call.message.answer_document(file)
    await call.answer()

@router.callback_query(F.data == "adm_exp_reqs")
async def adm_exp_reqs(call: CallbackQuery):
    if not is_admin(call.from_user.id): return

    with db.conn() as c:
        rows = c.execute("SELECT * FROM requests").fetchall()

    buf = io.StringIO()
    buf.write("id,user_id,status,code,amount,admin_id,created_at,resolved_at\n")
    for r in rows:
        buf.write(f"{r['id']},{r['user_id']},{r['status']},{r['code'] or ''},{r['amount'] or ''},{r['admin_id'] or ''},{r['created_at']},{r['resolved_at'] or ''}\n")

    file = BufferedInputFile(buf.getvalue().encode(), filename=f"requests_{int(datetime.utcnow().timestamp())}.csv")
    await call.message.answer_document(file)
    await call.answer()

@router.callback_query(F.data == "adm_exp_logs")
async def adm_exp_logs(call: CallbackQuery):
    if not is_admin(call.from_user.id): return

    with db.conn() as c:
        rows = c.execute("SELECT * FROM logs ORDER BY id DESC LIMIT 5000").fetchall()

    buf = io.StringIO()
    buf.write("id,user_id,action,detail,ts\n")
    for r in rows:
        buf.write(f"{r['id']},{r['user_id']},{r['action']},{r['detail']},{r['ts']}\n")

    file = BufferedInputFile(buf.getvalue().encode(), filename=f"logs_{int(datetime.utcnow().timestamp())}.csv")
    await call.message.answer_document(file)
    await call.answer()

# ============================================================
# ADMIN: SETTINGS
# ============================================================
@router.callback_query(F.data == "adm_settings")
async def adm_settings(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    limit = db.get_setting("request_limit_hours", "12")
    ref_bonus = db.get_setting("ref_bonus_gram", "5")
    ref_enabled = db.get_setting("ref_enabled", "1")

    txt = (
        f"⚙️ <b>Настройки</b>\n\n"
        f"⏱ Лимит заявок: <b>{limit} ч</b>\n"
        f"🎁 Бонус за рефа: <b>{ref_bonus} GRAM</b>\n"
        f"🔗 Рефералка: <b>{'вкл' if ref_enabled == '1' else 'выкл'}</b>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏱ Изменить лимит", callback_data="adm_set_limit")],
        [InlineKeyboardButton(text="🎁 Изменить бонус рефа", callback_data="adm_set_refbonus")],
        [InlineKeyboardButton(text="📝 Текст кнопки", callback_data="adm_set_btntext")],
        [InlineKeyboardButton(text="💬 Приветствие", callback_data="adm_set_welcome")],
        [InlineKeyboardButton(text="← Назад", callback_data="adm_back")],
    ])
    await call.message.edit_text(txt, reply_markup=kb)
    await call.answer()

@router.callback_query(F.data == "adm_set_btntext")
async def adm_set_btntext(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id): return
    await state.set_state(AdminStates.waiting_btn_text)
    await call.message.edit_text(f"📝 Текущий текст: <b>{btn_text()}</b>\n\nВведите новый текст кнопки:")
    await call.answer()

@router.message(AdminStates.waiting_btn_text)
async def adm_set_btntext_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    db.set_setting("btn_text", message.text)
    await message.answer("✅ Сохранено.")
    await state.clear()

@router.callback_query(F.data == "adm_set_welcome")
async def adm_set_welcome(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id): return
    await state.set_state(AdminStates.waiting_btn_text)
    await call.message.edit_text(f"💬 Текущее: <b>{welcome_text()}</b>\n\nВведите новый текст приветствия:")
    await call.answer()

@router.callback_query(F.data == "adm_set_limit")
async def adm_set_limit(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id): return
    await state.set_state(AdminStates.waiting_btn_text)
    await call.message.edit_text("⏱ Введите новый лимит в часах (например 12):")
    await call.answer()

# ============================================================
# ADMIN: MAINTENANCE
# ============================================================
@router.callback_query(F.data == "adm_maint")
async def adm_maint(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    state_val = is_maintenance()
    txt = (
        f"🛠 <b>Тех-перерыв</b>\n\n"
        f"Статус: {'🟢 Включён' if state_val else '🔴 Выключен'}\n\n"
        f"Сообщение:\n<i>{maintenance_text()}</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Выключить" if state_val else "Включить", callback_data="adm_maint_toggle")],
        [InlineKeyboardButton(text="✏️ Изменить текст", callback_data="adm_maint_text")],
        [InlineKeyboardButton(text="← Назад", callback_data="adm_back")],
    ])
    await call.message.edit_text(txt, reply_markup=kb)
    await call.answer()

@router.callback_query(F.data == "adm_maint_toggle")
async def adm_maint_toggle(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    new_val = "0" if is_maintenance() else "1"
    db.set_setting("maintenance", new_val)
    db.log(call.from_user.id, "maintenance", new_val)
    await adm_maint(call)

@router.callback_query(F.data == "adm_maint_text")
async def adm_maint_text(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id): return
    await state.set_state(AdminStates.waiting_maintenance_text)
    await call.message.edit_text("✏️ Введите новый текст тех-перерыва:")
    await call.answer()

@router.message(AdminStates.waiting_maintenance_text)
async def save_maint_text(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    db.set_setting("maintenance_text", message.text)
    await message.answer("✅ Текст сохранён.")
    await state.clear()

# ============================================================
# ADMIN: LOGS
# ============================================================
@router.callback_query(F.data == "adm_logs")
async def adm_logs(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    logs = db.recent_logs(30)
    txt = "📝 <b>Последние 30 событий</b>\n\n"
    for l in logs:
        ts = l["ts"][11:19] if l["ts"] else "—"
        txt += f"<code>{ts}</code> · {l['user_id']} · {l['action']} {l['detail'][:25]}\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← Назад", callback_data="adm_back")],
    ])
    await call.message.edit_text(txt, reply_markup=kb)
    await call.answer()

@router.callback_query(F.data == "adm_back")
async def adm_back(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    await call.message.edit_text("🔐 <b>Админ-панель</b>", reply_markup=admin_menu())
    await call.answer()

@router.callback_query(F.data == "adm_logout")
async def adm_logout(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    db.set_admin(call.from_user.id, 0)
    await call.message.edit_text("🚪 Вы вышли.")
    await call.answer()

# ============================================================
# RUN
# ============================================================
async def main():
    threading.Thread(target=run_http_server, daemon=True).start()
    asyncio.create_task(scheduler_loop())

    await bot.set_my_commands([
        BotCommand(command="start", description="Открыть Gram"),
        BotCommand(command="admin", description="Админ-панель"),
    ])
    print(f"HTTP server listening on port {PORT}")
    print("Bot started")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
