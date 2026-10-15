import os
import sqlite3
import asyncio
from datetime import datetime, timedelta
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    WebAppInfo, BotCommand
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
import aiohttp


# ============================================================
# CONFIG
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "1329")
MINI_APP_URL = os.getenv("MINI_APP_URL", "https://grammintora.space")
DB_PATH = os.getenv("DB_PATH", "bot.db")

WORKER_URL = os.getenv("WORKER_URL", "https://mintora-api.daetbrawl6868.workers.dev")
WORKER_KEY = os.getenv("WORKER_KEY", "mintora_secret_2025")

DIGEST_INTERVAL = int(os.getenv("DIGEST_INTERVAL", "3600"))

if not BOT_TOKEN:
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
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
            """)
            c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('maintenance', '0')")
            c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('maintenance_text', '🛠 Бот на техническом перерыве. Скоро вернёмся!')")
            c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('digest_enabled', '1')")
            c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('notify_activation', '0')")

    def upsert_user(self, user_id, username, first_name, last_name):
        now = datetime.utcnow().isoformat()
        with self.conn() as c:
            c.execute("""
                INSERT INTO users (user_id, username, first_name, last_name, joined_at, last_seen)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username=excluded.username, first_name=excluded.first_name,
                    last_name=excluded.last_name, last_seen=excluded.last_seen
            """, (user_id, username or "", first_name or "", last_name or "", now, now))

    def get_user(self, user_id):
        with self.conn() as c:
            row = c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            return dict(row) if row else None

    def list_users(self, limit=50, offset=0):
        with self.conn() as c:
            rows = c.execute("SELECT * FROM users ORDER BY joined_at DESC LIMIT ? OFFSET ?",
                             (limit, offset)).fetchall()
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
            if exclude_banned: q += " WHERE is_banned = 0"
            return [r["user_id"] for r in c.execute(q).fetchall()]

    def all_admins(self):
        with self.conn() as c:
            return [r["user_id"] for r in c.execute("SELECT user_id FROM users WHERE is_admin = 1").fetchall()]

    def log(self, user_id, action, detail=""):
        with self.conn() as c:
            c.execute("INSERT INTO logs (user_id, action, detail, ts) VALUES (?, ?, ?, ?)",
                      (user_id, action, detail, datetime.utcnow().isoformat()))

    def recent_logs(self, limit=50):
        with self.conn() as c:
            rows = c.execute("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]

    def get_setting(self, key, default=None):
        with self.conn() as c:
            row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default

    def set_setting(self, key, value):
        with self.conn() as c:
            c.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                      (key, str(value)))

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
    waiting_promo_maxuses = State()
    waiting_maintenance_text = State()


# ============================================================
# HELPERS
# ============================================================
def is_maintenance():
    return db.get_setting("maintenance", "0") == "1"

def maintenance_text():
    return db.get_setting("maintenance_text", "🛠 Бот на техническом перерыве.")

def digest_enabled():
    return db.get_setting("digest_enabled", "1") == "1"

def notify_activation():
    return db.get_setting("notify_activation", "0") == "1"

def is_admin(user_id):
    u = db.get_user(user_id)
    return bool(u and u["is_admin"])


def admin_menu():
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Аналитика", callback_data="adm_analytics")],
        [InlineKeyboardButton(text="🎟 Промокоды", callback_data="adm_promos")],
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="adm_users_0")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="adm_broadcast")],
        [InlineKeyboardButton(text="🔔 Уведомления", callback_data="adm_notify")],
        [InlineKeyboardButton(text="🛠 Тех-перерыв", callback_data="adm_maint")],
        [InlineKeyboardButton(text="📝 Логи бота", callback_data="adm_logs")],
        [InlineKeyboardButton(text="🚪 Выйти", callback_data="adm_logout")],
    ])
    return kb


# ============================================================
# WORKER API
# ============================================================
async def api_call(path, method="GET", body=None):
    url = f"{WORKER_URL}{path}"
    sep = "&" if "?" in url else "?"
    url += f"{sep}key={WORKER_KEY}"
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            if method == "POST":
                async with sess.post(url, json=body or {}) as r:
                    return await r.json()
            else:
                async with sess.get(url) as r:
                    return await r.json()
    except Exception as e:
        return {"ok": False, "error": "net", "msg": str(e)}


# ============================================================
# USER
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

    # реферальный параметр
    args = message.text.split(maxsplit=1)
    if len(args) > 1 and args[1].startswith("ref_"):
        ref_by = args[1][4:].strip()
        if ref_by and ref_by != str(u.id):
            try:
                await api_call("/user/ref", "POST", {"uid": str(u.id), "ref_by": ref_by})
                db.log(u.id, "ref_set", ref_by)
            except Exception:
                pass

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Открыть Mintora Wallet", web_app=WebAppInfo(url=MINI_APP_URL))],
        [InlineKeyboardButton(text="👤 Мой профиль", callback_data="user_me")],
    ])
    await message.answer(
        f"👋 Привет, <b>{u.first_name or 'друг'}</b>!\n\n"
        f"Добро пожаловать в <b>Mintora Wallet</b>.\n\n"
        f"Нажми на кнопку ниже, чтобы открыть приложение и активировать свой промокод.",
        reply_markup=kb
    )


@router.callback_query(F.data == "user_me")
async def cb_user_me(call: CallbackQuery):
    uid = str(call.from_user.id)
    d = await api_call(f"/user/balance?uid={uid}")
    if not d or not d.get("ok"):
        return await call.answer("Ошибка получения профиля", show_alert=True)

    balance = d.get("balance", 0)
    refs = d.get("refs", 0)
    activated = d.get("first_promo_activated", False)

    txt = (
        f"👤 <b>Мой профиль</b>\n\n"
        f"🆔 <code>{uid}</code>\n"
        f"💎 Баланс: <b>{balance}</b> GRAM\n"
        f"👥 Приглашено: <b>{refs}</b>\n"
        f"💰 Заработано с рефералов: <b>{round(refs * 0.8, 2)}</b> GRAM\n"
        f"📌 Статус: <b>{'Активен' if activated else 'Гость'}</b>\n\n"
        f"🔗 Твоя реф-ссылка:\n<code>https://t.me/{(await bot.me()).username}?start=ref_{uid}</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Открыть кошелёк", web_app=WebAppInfo(url=MINI_APP_URL))]
    ])
    await call.message.edit_text(txt, reply_markup=kb, disable_web_page_preview=True)
    await call.answer()


@router.message(Command("me"))
async def cmd_me(message: Message):
    uid = str(message.from_user.id)
    d = await api_call(f"/user/balance?uid={uid}")
    if not d or not d.get("ok"):
        return await message.answer("Ошибка получения профиля.")
    balance = d.get("balance", 0)
    refs = d.get("refs", 0)
    activated = d.get("first_promo_activated", False)
    bot_info = await bot.me()
    txt = (
        f"👤 <b>Мой профиль</b>\n\n"
        f"🆔 <code>{uid}</code>\n"
        f"💎 Баланс: <b>{balance}</b> GRAM\n"
        f"👥 Приглашено: <b>{refs}</b>\n"
        f"💰 С рефералов: <b>{round(refs * 0.8, 2)}</b> GRAM\n"
        f"📌 Статус: <b>{'Активен' if activated else 'Гость'}</b>\n\n"
        f"🔗 <code>https://t.me/{bot_info.username}?start=ref_{uid}</code>"
    )
    await message.answer(txt, disable_web_page_preview=True)


# ============================================================
# ADMIN LOGIN
# ============================================================
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
    try: await message.delete()
    except Exception: pass

    if (message.text or "").strip() == ADMIN_PASSWORD:
        db.set_admin(message.from_user.id, 1)
        db.log(message.from_user.id, "admin_login", "")
        await message.answer("✅ Доступ разрешён.\n\n🔐 <b>Админ-панель</b>", reply_markup=admin_menu())
    else:
        db.log(message.from_user.id, "admin_wrong_pass", "")
        await message.answer("❌ Неверный пароль.")
    await state.clear()


# ============================================================
# ADMIN NAV
# ============================================================
@router.callback_query(F.data == "adm_back")
async def cb_back(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer("Нет доступа")
    await call.message.edit_text("🔐 <b>Админ-панель</b>", reply_markup=admin_menu())
    await call.answer()


@router.callback_query(F.data == "adm_logout")
async def cb_logout(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer("Нет доступа")
    db.set_admin(call.from_user.id, 0)
    await call.message.edit_text("🚪 Вы вышли из админ-панели.")
    await call.answer()


# ============================================================
# ANALYTICS
# ============================================================
def fmt_analytics(d):
    if not d or not d.get("ok"):
        return "❌ Не удалось получить данные из воркера."

    p = d.get("promos", {})
    u = d.get("users", {})
    h1 = d.get("last_hour", {})
    h24 = d.get("last_24h", {})
    d7 = d.get("last_7d", {})

    return (
        f"📊 <b>Аналитика Mintora</b>\n"
        f"<i>{datetime.utcnow().strftime('%d.%m %H:%M')} UTC</i>\n\n"

        f"<b>👥 Пользователи</b>\n"
        f"  Всего: <b>{u.get('total',0)}</b>\n"
        f"  С балансом: <b>{u.get('with_balance',0)}</b>\n"
        f"  Общий баланс: <b>{u.get('total_balance',0)}</b> GRAM\n"
        f"  Рефералов: <b>{u.get('total_refs',0)}</b>\n\n"

        f"<b>🎟 Промокоды</b>\n"
        f"  Всего: <b>{p.get('total',0)}</b>\n"
        f"  Активных: <b>{p.get('active',0)}</b>\n"
        f"  Исчерпано: <b>{p.get('exhausted',0)}</b>\n"
        f"  Активаций: <b>{p.get('total_uses',0)}</b>\n\n"

        f"<b>⚡ Час</b>\n"
        f"  ✅ Активаций: <b>{h1.get('valid',0)}</b>\n"
        f"  ❌ Неверных: <b>{h1.get('invalid',0)}</b>\n"
        f"  👤 Уникальных: <b>{h1.get('unique',0)}</b>\n\n"

        f"<b>📅 24 часа</b>\n"
        f"  ✅ Активаций: <b>{h24.get('valid',0)}</b>\n"
        f"  🎁 Рефералов: <b>{h24.get('refs',0)}</b>\n"
        f"  👤 Уникальных: <b>{h24.get('unique',0)}</b>\n\n"

        f"<b>📆 7 дней</b>\n"
        f"  ✅ Активаций: <b>{d7.get('valid',0)}</b>\n"
        f"  🎁 Рефералов: <b>{d7.get('refs',0)}</b>\n"
        f"  👤 Уникальных: <b>{d7.get('unique',0)}</b>"
    )


@router.callback_query(F.data == "adm_analytics")
async def cb_analytics(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer("Нет доступа")
    await call.answer("Загружаю...")
    d = await api_call("/admin/analytics")
    txt = fmt_analytics(d)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="adm_analytics")],
        [InlineKeyboardButton(text="📜 Последние события", callback_data="adm_recent")],
        [InlineKeyboardButton(text="← Назад", callback_data="adm_back")],
    ])
    try:
        await call.message.edit_text(txt, reply_markup=kb)
    except Exception:
        await call.message.answer(txt, reply_markup=kb)


@router.callback_query(F.data == "adm_recent")
async def cb_recent(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer("Нет доступа")
    d = await api_call("/admin/analytics")
    if not d or not d.get("ok"):
        return await call.answer("Ошибка", show_alert=True)
    txt = "📜 <b>Последние события</b>\n\n"
    for l in d.get("recent", [])[:15]:
        t = datetime.utcfromtimestamp(l["ts"] / 1000).strftime("%H:%M:%S")
        txt += f"<code>{t}</code> · <b>{l.get('event','?')}</b> · {str(l.get('detail',''))[:40]}\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← Назад", callback_data="adm_analytics")],
    ])
    await call.message.edit_text(txt, reply_markup=kb)
    await call.answer()


# ============================================================
# NOTIFICATIONS
# ============================================================
@router.callback_query(F.data == "adm_notify")
async def cb_notify(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer("Нет доступа")
    txt = (
        f"🔔 <b>Уведомления</b>\n\n"
        f"📨 Авто-дайджест раз в час: {'🟢 ВКЛ' if digest_enabled() else '🔴 ВЫКЛ'}\n"
        f"⚡ Пинг при активации кода: {'🟢 ВКЛ' if notify_activation() else '🔴 ВЫКЛ'}\n\n"
        f"<i>Дайджест приходит всем админам.</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{'🔴 Выключить' if digest_enabled() else '🟢 Включить'} дайджест",
                              callback_data="adm_notify_digest")],
        [InlineKeyboardButton(text=f"{'🔴 Выключить' if notify_activation() else '🟢 Включить'} пинг",
                              callback_data="adm_notify_activation")],
        [InlineKeyboardButton(text="← Назад", callback_data="adm_back")],
    ])
    await call.message.edit_text(txt, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data == "adm_notify_digest")
async def cb_notify_digest(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    db.set_setting("digest_enabled", "0" if digest_enabled() else "1")
    await cb_notify(call)


@router.callback_query(F.data == "adm_notify_activation")
async def cb_notify_activation(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    db.set_setting("notify_activation", "0" if notify_activation() else "1")
    await cb_notify(call)


# ============================================================
# USERS
# ============================================================
@router.callback_query(F.data.startswith("adm_users_"))
async def cb_users(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer("Нет доступа")
    page = int(call.data.split("_")[-1])
    users = db.list_users(limit=10, offset=page * 10)
    total = db.count_users()
    if not users:
        return await call.answer("Пусто", show_alert=True)

    buttons = []
    for u in users:
        name = u["first_name"] or "—"
        status = "🚫" if u["is_banned"] else ("👑" if u["is_admin"] else "✅")
        buttons.append([InlineKeyboardButton(
            text=f"{status} {name[:25]}",
            callback_data=f"adm_user_{u['user_id']}"
        )])

    nav = []
    if page > 0: nav.append(InlineKeyboardButton(text="← Пред", callback_data=f"adm_users_{page-1}"))
    if (page + 1) * 10 < total: nav.append(InlineKeyboardButton(text="След →", callback_data=f"adm_users_{page+1}"))
    if nav: buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="← Назад", callback_data="adm_back")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await call.message.edit_text(f"👥 <b>Пользователи</b> ({page*10+1}-{page*10+len(users)} из {total})",
                                 reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("adm_user_"))
async def cb_user(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer("Нет доступа")
    uid = int(call.data.split("_")[-1])
    u = db.get_user(uid)
    if not u: return await call.answer("Не найден", show_alert=True)

    d = await api_call(f"/user/balance?uid={uid}")
    balance = 0; refs = 0; activated = False
    if d and d.get("ok"):
        balance = d.get("balance", 0)
        refs = d.get("refs", 0)
        activated = d.get("first_promo_activated", False)

    name = u["first_name"] or "—"
    username = f"@{u['username']}" if u["username"] else "—"
    status = "🚫 Забанен" if u["is_banned"] else ("👑 Админ" if u["is_admin"] else "✅ Активен")
    joined = u["joined_at"][:16].replace("T", " ") if u["joined_at"] else "—"
    seen = u["last_seen"][:16].replace("T", " ") if u["last_seen"] else "—"

    txt = (
        f"👤 <b>{name}</b>\n🔗 {username}\n🆔 <code>{uid}</code>\n"
        f"📅 Зашёл: {joined}\n👀 Был: {seen}\n📌 {status}\n\n"
        f"💎 Баланс: <b>{balance}</b> GRAM\n"
        f"👥 Рефералов: <b>{refs}</b>\n"
        f"🎟 Активация: <b>{'Да' if activated else 'Нет'}</b>"
    )

    btns = []
    if u["is_banned"]:
        btns.append([InlineKeyboardButton(text="✅ Разбанить", callback_data=f"adm_unban_{uid}")])
    else:
        btns.append([InlineKeyboardButton(text="🚫 Забанить", callback_data=f"adm_ban_{uid}")])
    btns.append([InlineKeyboardButton(text="✉️ Написать", callback_data=f"adm_dm_{uid}")])
    btns.append([InlineKeyboardButton(text="← Назад", callback_data="adm_users_0")])
    await call.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
    await call.answer()


@router.callback_query(F.data.startswith("adm_ban_"))
async def cb_ban(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    uid = int(call.data.split("_")[-1])
    db.set_ban(uid, True)
    db.log(call.from_user.id, "ban_user", str(uid))
    await call.answer("Забанен")
    await cb_user(call)


@router.callback_query(F.data.startswith("adm_unban_"))
async def cb_unban(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    uid = int(call.data.split("_")[-1])
    db.set_ban(uid, False)
    db.log(call.from_user.id, "unban_user", str(uid))
    await call.answer("Разбанен")
    await cb_user(call)


@router.callback_query(F.data.startswith("adm_dm_"))
async def cb_dm(call: CallbackQuery, state: FSMContext):
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
# BROADCAST
# ============================================================
@router.callback_query(F.data == "adm_broadcast")
async def cb_broadcast(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id): return
    await state.set_state(AdminStates.waiting_broadcast)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← Отмена", callback_data="adm_back")]
    ])
    await call.message.edit_text("📢 Введите текст рассылки:", reply_markup=kb)
    await call.answer()


@router.message(AdminStates.waiting_broadcast)
async def do_broadcast(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    ids = db.all_user_ids(exclude_banned=False)
    sent = 0; failed = 0
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


# ============================================================
# MAINTENANCE
# ============================================================
@router.callback_query(F.data == "adm_maint")
async def cb_maint(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    state_val = is_maintenance()
    txt = (f"🛠 <b>Тех-перерыв</b>\n\n"
           f"Статус: {'🟢 Включён' if state_val else '🔴 Выключен'}\n\n"
           f"Сообщение:\n<i>{maintenance_text()}</i>")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Выключить" if state_val else "Включить", callback_data="adm_maint_toggle")],
        [InlineKeyboardButton(text="✏️ Изменить текст", callback_data="adm_maint_text")],
        [InlineKeyboardButton(text="← Назад", callback_data="adm_back")],
    ])
    await call.message.edit_text(txt, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data == "adm_maint_toggle")
async def cb_maint_toggle(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    new_val = "0" if is_maintenance() else "1"
    db.set_setting("maintenance", new_val)
    db.log(call.from_user.id, "maintenance", new_val)
    await cb_maint(call)


@router.callback_query(F.data == "adm_maint_text")
async def cb_maint_text(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id): return
    await state.set_state(AdminStates.waiting_maintenance_text)
    await call.message.edit_text("✏️ Введите новый текст:")
    await call.answer()


@router.message(AdminStates.waiting_maintenance_text)
async def save_maint_text(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    db.set_setting("maintenance_text", message.text)
    await message.answer("✅ Текст сохранён.")
    await state.clear()


# ============================================================
# PROMO CODES (через воркер)
# ============================================================
@router.callback_query(F.data == "adm_promos")
async def cb_promos(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer("Нет доступа")
    await call.answer("Загружаю...")
    d = await api_call("/admin/promo/list")
    if not d or not d.get("ok"):
        return await call.message.edit_text("❌ Ошибка загрузки кодов.")

    promos = d.get("promos", [])
    txt = "🎟 <b>Промокоды</b>\n\n"
    if not promos:
        txt += "<i>Пусто</i>"
    else:
        for p in promos[:15]:
            used = p.get("used", 0)
            mx = p.get("max_uses", 0)
            mx_disp = "∞" if mx >= 999999 else mx
            status = "🟢" if used < mx else "🔴"
            txt += f"{status} <code>{p['code']}</code> · +{p['amount']} GRAM · {used}/{mx_disp}\n"
        if len(promos) > 15:
            txt += f"\n...и ещё {len(promos) - 15}"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать", callback_data="adm_promo_new")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="adm_promo_del")],
        [InlineKeyboardButton(text="🔄 Сбросить счётчик", callback_data="adm_promo_reset_list")],
        [InlineKeyboardButton(text="← Назад", callback_data="adm_back")],
    ])
    await call.message.edit_text(txt, reply_markup=kb)


@router.callback_query(F.data == "adm_promo_new")
async def cb_promo_new(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id): return
    await state.set_state(AdminStates.waiting_promo_code)
    await call.message.edit_text("🎟 Введите <b>код</b> (3-20 символов, A-Z 0-9):")
    await call.answer()


@router.message(AdminStates.waiting_promo_code)
async def promo_code_step(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    code = (message.text or "").strip().upper()
    if not code.isalnum() or len(code) < 3 or len(code) > 20:
        return await message.answer("❌ Только буквы/цифры, 3-20 символов. Ещё раз:")
    await state.update_data(promo_code=code)
    await state.set_state(AdminStates.waiting_promo_amount)
    await message.answer(f"Код: <code>{code}</code>\n\n💰 Введите сумму GRAM (целое):")


@router.message(AdminStates.waiting_promo_amount)
async def promo_amount_step(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    try:
        amount = int((message.text or "").strip())
        if amount <= 0 or amount > 100000: raise ValueError()
    except Exception:
        return await message.answer("❌ Введите целое число 1-100000:")
    await state.update_data(promo_amount=amount)
    await state.set_state(AdminStates.waiting_promo_maxuses)
    await message.answer("🔢 Введите <b>лимит активаций</b> (целое). <code>0</code> = без лимита:")


@router.message(AdminStates.waiting_promo_maxuses)
async def promo_maxuses_step(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    try:
        mx = int((message.text or "").strip())
        if mx < 0 or mx > 1000000: raise ValueError()
    except Exception:
        return await message.answer("❌ Введите целое 0-1000000:")

    data = await state.get_data()
    code = data.get("promo_code")
    amount = data.get("promo_amount")

    d = await api_call("/admin/promo/create", method="POST",
                       body={"code": code, "amount": amount, "max_uses": mx})
    if not d or not d.get("ok"):
        err = (d or {}).get("error", "unknown")
        return await message.answer(f"❌ Ошибка: {err}")

    mx_disp = "∞" if mx == 0 else mx
    await message.answer(
        f"✅ <b>Промокод создан</b>\n\n"
        f"Код: <code>{code}</code>\nСумма: +{amount} GRAM\nЛимит: {mx_disp}"
    )
    db.log(message.from_user.id, "promo_created", f"{code}={amount} max={mx}")
    await state.clear()


@router.callback_query(F.data == "adm_promo_del")
async def cb_promo_del(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id): return
    d = await api_call("/admin/promo/list")
    if not d or not d.get("ok"): return await call.answer("Ошибка")
    promos = d.get("promos", [])
    if not promos: return await call.answer("Пусто", show_alert=True)
    buttons = [[InlineKeyboardButton(text=f"🗑 {p['code']}", callback_data=f"adm_promo_delx_{p['code']}")] for p in promos[:30]]
    buttons.append([InlineKeyboardButton(text="← Назад", callback_data="adm_promos")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await call.message.edit_text("🗑 Выберите код для удаления:", reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("adm_promo_delx_"))
async def cb_promo_delx(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    code = call.data.split("adm_promo_delx_", 1)[1]
    await api_call("/admin/promo/delete", method="POST", body={"code": code})
    db.log(call.from_user.id, "promo_deleted", code)
    await call.answer(f"Удалён: {code}")
    await cb_promos(call)


@router.callback_query(F.data == "adm_promo_reset_list")
async def cb_promo_reset_list(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    d = await api_call("/admin/promo/list")
    if not d or not d.get("ok"): return await call.answer("Ошибка")
    promos = d.get("promos", [])
    if not promos: return await call.answer("Пусто", show_alert=True)
    buttons = [[InlineKeyboardButton(text=f"🔄 {p['code']}", callback_data=f"adm_promo_resetx_{p['code']}")] for p in promos[:30]]
    buttons.append([InlineKeyboardButton(text="← Назад", callback_data="adm_promos")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await call.message.edit_text("🔄 Выберите код для сброса счётчика:", reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("adm_promo_resetx_"))
async def cb_promo_resetx(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    code = call.data.split("adm_promo_resetx_", 1)[1]
    await api_call("/admin/promo/reset", method="POST", body={"code": code})
    await call.answer(f"Сброшен: {code}")
    await cb_promos(call)


# ============================================================
# BOT LOGS
# ============================================================
@router.callback_query(F.data == "adm_logs")
async def cb_logs(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    logs = db.recent_logs(30)
    txt = "📝 <b>Логи бота (30)</b>\n\n"
    for l in logs:
        ts = l["ts"][11:19]
        txt += f"<code>{ts}</code> · {l['user_id']} · {l['action']} {l['detail'][:30]}\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← Назад", callback_data="adm_back")]
    ])
    await call.message.edit_text(txt, reply_markup=kb)
    await call.answer()


# ============================================================
# HOURLY DIGEST LOOP
# ============================================================
async def digest_loop():
    await asyncio.sleep(60)
    while True:
        try:
            if digest_enabled():
                d = await api_call("/admin/analytics/hourly")
                if d and d.get("ok"):
                    txt = (
                        f"📊 <b>Mintora · Час</b>\n\n"
                        f"✅ Активаций: <b>{d.get('valid', 0)}</b>\n"
                        f"❌ Неверных: <b>{d.get('invalid', 0)}</b>\n"
                        f"🎁 Рефералов: <b>{d.get('refs', 0)}</b>\n"
                        f"👤 Уникальных: <b>{d.get('unique', 0)}</b>\n"
                        f"💎 GRAM выдано: <b>{d.get('gram_promised', 0)}</b>"
                    )
                    for admin_id in db.all_admins():
                        try:
                            await bot.send_message(admin_id, txt)
                        except Exception:
                            pass
        except Exception:
            pass
        await asyncio.sleep(DIGEST_INTERVAL)


# ============================================================
# RUN
# ============================================================
async def main():
    await bot.set_my_commands([
        BotCommand(command="start", description="Открыть Mintora Wallet"),
        BotCommand(command="me", description="Мой профиль"),
        BotCommand(command="admin", description="Админ-панель"),
    ])
    asyncio.create_task(digest_loop())
    print("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
