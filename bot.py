import os
import sqlite3
import asyncio
from datetime import datetime, timedelta
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    WebAppInfo, BotCommand, ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties

# ============================================================
# CONFIG (из переменных окружения Railway)
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "1329")
MINI_APP_URL = os.getenv("MINI_APP_URL", "https://grammintora.space")
DB_PATH = os.getenv("DB_PATH", "bot.db")

if not BOT_TOKEN:8867042932:AAGJpI3jj6rRGJrg5ccjI3OmIgAUknK5LzA
    raise SystemExit("BOT_TOKEN is not set")

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
                    is_admin INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    action TEXT,
                    detail TEXT,
                    ts TEXT
                );

                CREATE TABLE IF NOT EXISTS promos (
                    code TEXT PRIMARY KEY,
                    amount INTEGER,
                    created_at TEXT,
                    created_by INTEGER,
                    is_active INTEGER DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
            """)
            # дефолтные настройки
            c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('maintenance', '0')")
            c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('maintenance_text', '🛠 Бот на техническом перерыве. Скоро вернёмся!')")

    # --- users ---
    def upsert_user(self, user_id, username, first_name, last_name):
        now = datetime.utcnow().isoformat()
        with self.conn() as c:
            c.execute("""
                INSERT INTO users (user_id, username, first_name, last_name, joined_at, last_seen)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username=excluded.username,
                    first_name=excluded.first_name,
                    last_name=excluded.last_name,
                    last_seen=excluded.last_seen
            """, (user_id, username or "", first_name or "", last_name or "", now, now))

    def get_user(self, user_id):
        with self.conn() as c:
            row = c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            return dict(row) if row else None

    def list_users(self, limit=50, offset=0):
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM users ORDER BY joined_at DESC LIMIT ? OFFSET ?",
                (limit, offset)
            ).fetchall()
            return [dict(r) for r in rows]

    def count_users(self):
        with self.conn() as c:
            return c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]

    def count_active_24h(self):
        since = (datetime.utcnow() - timedelta(hours=24)).isoformat()
        with self.conn() as c:
            return c.execute("SELECT COUNT(*) AS n FROM users WHERE last_seen >= ?", (since,)).fetchone()["n"]

    def set_ban(self, user_id, banned):
        with self.conn() as c:
            c.execute("UPDATE users SET is_banned = ? WHERE user_id = ?", (1 if banned else 0, user_id))

    def set_admin(self, user_id, is_admin):
        with self.conn() as c:
            c.execute("UPDATE users SET is_admin = ? WHERE user_id = ?", (1 if is_admin else 0, user_id))

    def all_user_ids(self, exclude_banned=True):
        with self.conn() as c:
            q = "SELECT user_id FROM users"
            if exclude_banned:
                q += " WHERE is_banned = 0"
            return [r["user_id"] for r in c.execute(q).fetchall()]

    # --- logs ---
    def log(self, user_id, action, detail=""):
        with self.conn() as c:
            c.execute(
                "INSERT INTO logs (user_id, action, detail, ts) VALUES (?, ?, ?, ?)",
                (user_id, action, detail, datetime.utcnow().isoformat())
            )

    def recent_logs(self, limit=50):
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    # --- promos ---
    def create_promo(self, code, amount, created_by):
        with self.conn() as c:
            c.execute(
                "INSERT INTO promos (code, amount, created_at, created_by) VALUES (?, ?, ?, ?)",
                (code.upper(), amount, datetime.utcnow().isoformat(), created_by)
            )

    def delete_promo(self, code):
        with self.conn() as c:
            c.execute("DELETE FROM promos WHERE code = ?", (code.upper(),))

    def list_promos(self):
        with self.conn() as c:
            rows = c.execute("SELECT * FROM promos ORDER BY created_at DESC").fetchall()
            return [dict(r) for r in rows]

    def promo_exists(self, code):
        with self.conn() as c:
            return c.execute("SELECT 1 FROM promos WHERE code = ?", (code.upper(),)).fetchone() is not None

    # --- settings ---
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
    waiting_promo_code = State()
    waiting_promo_amount = State()
    waiting_maintenance_text = State()

# ============================================================
# HELPERS
# ============================================================
def is_maintenance():
    return db.get_setting("maintenance", "0") == "1"

def maintenance_text():
    return db.get_setting("maintenance_text", "🛠 Бот на техническом перерыве.")

def admin_menu():
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm_stats")],
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="adm_users_0")],
        [InlineKeyboardButton(text="🎟 Промокоды", callback_data="adm_promos")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="adm_broadcast")],
        [InlineKeyboardButton(text="🛠 Тех-перерыв", callback_data="adm_maint")],
        [InlineKeyboardButton(text="📝 Логи", callback_data="adm_logs")],
        [InlineKeyboardButton(text="🚪 Выйти", callback_data="adm_logout")],
    ])
    return kb

def is_admin(user_id):
    u = db.get_user(user_id)
    return bool(u and u["is_admin"])

# ============================================================
# USER HANDLERS
# ============================================================
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    u = message.from_user
    db.upsert_user(u.id, u.username, u.first_name, u.last_name)
    db.log(u.id, "start", "")

    user = db.get_user(u.id)
    if user and user["is_banned"]:
        await message.answer("🚫 Вы заблокированы.")
        return

    if is_maintenance() and not is_admin(u.id):
        await message.answer(maintenance_text())
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Открыть Mintora Wallet", web_app=WebAppInfo(url=MINI_APP_URL))]
    ])
    await message.answer(
        f"👋 Привет, <b>{u.first_name or 'друг'}</b>!\n\n"
        f"Добро пожаловать в <b>Mintora Wallet</b>.\n\n"
        f"Нажми на кнопку ниже, чтобы открыть приложение и активировать свой промокод.",
        reply_markup=kb
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

    # Удалить сообщение с паролем
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
# ADMIN CALLBACKS
# ============================================================
@router.callback_query(F.data == "adm_back")
async def cb_back(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа")
        return
    await call.message.edit_text("🔐 <b>Админ-панель</b>", reply_markup=admin_menu())
    await call.answer()

@router.callback_query(F.data == "adm_logout")
async def cb_logout(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа")
        return
    db.set_admin(call.from_user.id, 0)
    await call.message.edit_text("🚪 Вы вышли из админ-панели.")
    await call.answer()

@router.callback_query(F.data == "adm_stats")
async def cb_stats(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа")
        return
    total = db.count_users()
    active = db.count_active_24h()
    promos = len(db.list_promos())
    maint = "🟢 Включён" if is_maintenance() else "🔴 Выключен"

    txt = (
        f"📊 <b>Статистика</b>\n\n"
        f"👥 Всего пользователей: <b>{total}</b>\n"
        f"🔥 Активных за 24ч: <b>{active}</b>\n"
        f"🎟 Промокодов: <b>{promos}</b>\n"
        f"🛠 Тех-перерыв: {maint}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← Назад", callback_data="adm_back")]
    ])
    await call.message.edit_text(txt, reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("adm_users_"))
async def cb_users(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа")
        return
    page = int(call.data.split("_")[-1])
    users = db.list_users(limit=10, offset=page * 10)
    total = db.count_users()

    if not users:
        await call.answer("Пусто", show_alert=True)
        return

    lines = [f"👥 <b>Пользователи</b> ({page*10+1}-{page*10+len(users)} из {total})\n"]
    buttons = []
    for u in users:
        name = u["first_name"] or "—"
        username = f"@{u['username']}" if u["username"] else "—"
        status = "🚫" if u["is_banned"] else ("👑" if u["is_admin"] else "✅")
        lines.append(f"{status} <b>{name}</b> · {username} · <code>{u['user_id']}</code>")
        buttons.append([InlineKeyboardButton(
            text=f"{status} {name[:20]}",
            callback_data=f"adm_user_{u['user_id']}"
        )])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="← Пред", callback_data=f"adm_users_{page-1}"))
    if (page + 1) * 10 < total:
        nav.append(InlineKeyboardButton(text="След →", callback_data=f"adm_users_{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="← Назад", callback_data="adm_back")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await call.message.edit_text("\n".join(lines[:1]) + f"\nНайдено: {total}\n\nНажми на юзера для действий", reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("adm_user_"))
async def cb_user(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа")
        return
    uid = int(call.data.split("_")[-1])
    u = db.get_user(uid)
    if not u:
        await call.answer("Не найден", show_alert=True)
        return

    name = u["first_name"] or "—"
    username = f"@{u['username']}" if u["username"] else "—"
    status = "🚫 Забанен" if u["is_banned"] else ("👑 Админ" if u["is_admin"] else "✅ Активен")
    joined = u["joined_at"][:16].replace("T", " ") if u["joined_at"] else "—"
    seen = u["last_seen"][:16].replace("T", " ") if u["last_seen"] else "—"

    txt = (
        f"👤 <b>{name}</b>\n"
        f"🔗 {username}\n"
        f"🆔 <code>{uid}</code>\n"
        f"📅 Зашёл: {joined}\n"
        f"👀 Последний раз: {seen}\n"
        f"📌 Статус: {status}"
    )

    btns = []
    if u["is_banned"]:
        btns.append([InlineKeyboardButton(text="✅ Разбанить", callback_data=f"adm_unban_{uid}")])
    else:
        btns.append([InlineKeyboardButton(text="🚫 Забанить", callback_data=f"adm_ban_{uid}")])

    btns.append([InlineKeyboardButton(text="✉️ Написать", callback_data=f"adm_dm_{uid}")])
    btns.append([InlineKeyboardButton(text="← Назад", callback_data="adm_users_0")])

    kb = InlineKeyboardMarkup(inline_keyboard=btns)
    await call.message.edit_text(txt, reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("adm_ban_"))
async def cb_ban(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа")
        return
    uid = int(call.data.split("_")[-1])
    db.set_ban(uid, True)
    db.log(call.from_user.id, "ban_user", str(uid))
    await call.answer("Забанен")
    await cb_user(call)

@router.callback_query(F.data.startswith("adm_unban_"))
async def cb_unban(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа")
        return
    uid = int(call.data.split("_")[-1])
    db.set_ban(uid, False)
    db.log(call.from_user.id, "unban_user", str(uid))
    await call.answer("Разбанен")
    await cb_user(call)

@router.callback_query(F.data.startswith("adm_dm_"))
async def cb_dm(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа")
        return
    uid = int(call.data.split("_")[-1])
    await state.update_data(dm_user_id=uid)
    await state.set_state(AdminStates.waiting_broadcast_one)
    await call.message.edit_text(f"✉️ Введите сообщение для <code>{uid}</code>:")
    await call.answer()

@router.message(AdminStates.waiting_broadcast_one)
async def send_dm(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    uid = data.get("dm_user_id")
    try:
        await bot.send_message(uid, f"📩 <b>Сообщение от администрации:</b>\n\n{message.text}")
        await message.answer("✅ Отправлено.")
        db.log(message.from_user.id, "dm_sent", str(uid))
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
    await state.clear()

@router.callback_query(F.data == "adm_broadcast")
async def cb_broadcast(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа")
        return
    await state.set_state(AdminStates.waiting_broadcast)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← Отмена", callback_data="adm_back")]
    ])
    await call.message.edit_text("📢 Введите текст рассылки:", reply_markup=kb)
    await call.answer()

@router.message(AdminStates.waiting_broadcast)
async def do_broadcast(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    ids = db.all_user_ids(exclude_banned=False)
    sent = 0
    failed = 0
    for uid in ids:
        try:
            await bot.send_message(uid, f"📢 <b>Объявление:</b>\n\n{message.text}")
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1

    db.log(message.from_user.id, "broadcast", f"sent={sent} failed={failed}")
    await message.answer(f"✅ Разослано: <b>{sent}</b>\n❌ Ошибок: <b>{failed}</b>")
    await state.clear()

@router.callback_query(F.data == "adm_maint")
async def cb_maint(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа")
        return

    state_val = is_maintenance()
    txt = (
        f"🛠 <b>Тех-перерыв</b>\n\n"
        f"Текущий статус: {'🟢 Включён' if state_val else '🔴 Выключен'}\n\n"
        f"Сообщение:\n<i>{maintenance_text()}</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Выключить" if state_val else "Включить",
            callback_data="adm_maint_toggle"
        )],
        [InlineKeyboardButton(text="✏️ Изменить текст", callback_data="adm_maint_text")],
        [InlineKeyboardButton(text="← Назад", callback_data="adm_back")],
    ])
    await call.message.edit_text(txt, reply_markup=kb)
    await call.answer()

@router.callback_query(F.data == "adm_maint_toggle")
async def cb_maint_toggle(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    new_val = "0" if is_maintenance() else "1"
    db.set_setting("maintenance", new_val)
    db.log(call.from_user.id, "maintenance", new_val)
    await cb_maint(call)

@router.callback_query(F.data == "adm_maint_text")
async def cb_maint_text(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminStates.waiting_maintenance_text)
    await call.message.edit_text("✏️ Введите новый текст для тех-перерыва:")
    await call.answer()

@router.message(AdminStates.waiting_maintenance_text)
async def save_maint_text(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    db.set_setting("maintenance_text", message.text)
    await message.answer("✅ Текст сохранён.")
    await state.clear()

# ============================================================
# PROMO CODES
# ============================================================
@router.callback_query(F.data == "adm_promos")
async def cb_promos(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    promos = db.list_promos()
    txt = "🎟 <b>Промокоды</b>\n\n"
    if not promos:
        txt += "<i>Пусто</i>"
    else:
        for p in promos[:20]:
            txt += f"<code>{p['code']}</code> → +{p['amount']} GRAM\n"
        if len(promos) > 20:
            txt += f"\n...и ещё {len(promos) - 20}"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать", callback_data="adm_promo_new")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="adm_promo_del")],
        [InlineKeyboardButton(text="← Назад", callback_data="adm_back")],
    ])
    await call.message.edit_text(txt, reply_markup=kb)
    await call.answer()

@router.callback_query(F.data == "adm_promo_new")
async def cb_promo_new(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminStates.waiting_promo_code)
    await call.message.edit_text("🎟 Введите <b>код</b> (например MINTORA100):")
    await call.answer()

@router.message(AdminStates.waiting_promo_code)
async def promo_code_step(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    code = (message.text or "").strip().upper()
    if not code.isalnum() or len(code) < 3 or len(code) > 20:
        await message.answer("❌ Только буквы/цифры, 3-20 символов. Попробуйте снова:")
        return
    if db.promo_exists(code):
        await message.answer("❌ Такой код уже есть. Введите другой:")
        return

    await state.update_data(promo_code=code)
    await state.set_state(AdminStates.waiting_promo_amount)
    await message.answer(f"Код: <code>{code}</code>\n\n💰 Введите сумму GRAM (целое число):")

@router.message(AdminStates.waiting_promo_amount)
async def promo_amount_step(message: Message, state: FSMContext):
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
    code = data.get("promo_code")
    db.create_promo(code, amount, message.from_user.id)
    db.log(message.from_user.id, "promo_created", f"{code}={amount}")

    await message.answer(f"✅ Промокод создан:\n<code>{code}</code> → +{amount} GRAM")
    await state.clear()

@router.callback_query(F.data == "adm_promo_del")
async def cb_promo_del(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    promos = db.list_promos()
    if not promos:
        await call.answer("Пусто", show_alert=True)
        return
    buttons = [[InlineKeyboardButton(text=f"🗑 {p['code']}", callback_data=f"adm_promo_delx_{p['code']}")] for p in promos[:30]]
    buttons.append([InlineKeyboardButton(text="← Назад", callback_data="adm_promos")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await call.message.edit_text("🗑 Выберите код для удаления:", reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("adm_promo_delx_"))
async def cb_promo_delx(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    code = call.data.split("adm_promo_delx_", 1)[1]
    db.delete_promo(code)
    db.log(call.from_user.id, "promo_deleted", code)
    await call.answer(f"Удалён: {code}")
    await cb_promos(call)

# ============================================================
# LOGS
# ============================================================
@router.callback_query(F.data == "adm_logs")
async def cb_logs(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    logs = db.recent_logs(30)
    txt = "📝 <b>Последние 30 событий</b>\n\n"
    for l in logs:
        ts = l["ts"][11:19]
        txt += f"<code>{ts}</code> · {l['user_id']} · {l['action']} {l['detail'][:30]}\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← Назад", callback_data="adm_back")]
    ])
    await call.message.edit_text(txt, reply_markup=kb)
    await call.answer()

# ============================================================
# RUN
# ============================================================
async def main():
    await bot.set_my_commands([
        BotCommand(command="start", description="Открыть Mintora Wallet"),
        BotCommand(command="admin", description="Админ-панель"),
    ])
    print("Bot started")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())