"""EvuMusic Telegram bot — aiogram 3.x + asyncpg"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
from datetime import datetime, timezone
from html import escape
from typing import Any, Awaitable, Callable, Dict

import asyncpg
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
    WebAppInfo,
)
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("evumusic")

# ─── Config ───
BOT_TOKEN    = os.getenv("BOT_TOKEN")
WEBAPP_URL   = (os.getenv("WEBAPP_URL") or "").rstrip("/")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_IDS = [
    int(x) for x in re.split(r"[,\s]+", os.getenv("ADMIN_IDS") or "")
    if x.strip().isdigit()
]

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not WEBAPP_URL:
    raise RuntimeError("WEBAPP_URL is not set")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")
if not ADMIN_IDS:
    log.warning("ADMIN_IDS is empty — админ-команды будут недоступны")

pool: asyncpg.Pool | None = None

# ─── Schema ───
SQL_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id                   BIGSERIAL PRIMARY KEY,
  telegram_id          BIGINT UNIQUE NOT NULL,
  username             TEXT,
  first_name           TEXT,
  last_name            TEXT,
  language_code        TEXT DEFAULT 'ru',
  theme                TEXT DEFAULT 'blue' CHECK (theme IN ('blue','white','black')),
  is_admin             BOOLEAN DEFAULT FALSE,
  is_banned            BOOLEAN DEFAULT FALSE,
  invited_by           BIGINT REFERENCES users(telegram_id),
  subscription_until   TIMESTAMPTZ,
  subscription_type    TEXT,
  created_at           TIMESTAMPTZ DEFAULT now(),
  last_seen_at         TIMESTAMPTZ DEFAULT now()
);
ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_until TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_type  TEXT;

CREATE TABLE IF NOT EXISTS invite_codes (
  code        TEXT PRIMARY KEY,
  created_by  BIGINT NOT NULL REFERENCES users(telegram_id),
  used_by     BIGINT UNIQUE REFERENCES users(telegram_id),
  created_at  TIMESTAMPTZ DEFAULT now(),
  used_at     TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS subscription_keys (
  code            TEXT PRIMARY KEY,
  duration_days   INT,
  duration_label  TEXT NOT NULL,
  created_by      BIGINT NOT NULL REFERENCES users(telegram_id),
  created_at      TIMESTAMPTZ DEFAULT now(),
  used_by         BIGINT REFERENCES users(telegram_id),
  used_at         TIMESTAMPTZ,
  note            TEXT
);
CREATE INDEX IF NOT EXISTS idx_subkeys_created ON subscription_keys(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_subkeys_used    ON subscription_keys(used_by);

CREATE TABLE IF NOT EXISTS library_tracks (
  id          BIGSERIAL PRIMARY KEY,
  user_id     BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
  track_id    BIGINT NOT NULL,
  title       TEXT NOT NULL,
  artist      TEXT,
  duration    TEXT,
  added_at    TIMESTAMPTZ DEFAULT now(),
  UNIQUE (user_id, track_id)
);

CREATE TABLE IF NOT EXISTS favorites (
  user_id     BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
  track_id    BIGINT NOT NULL,
  title       TEXT NOT NULL,
  artist      TEXT,
  duration    TEXT,
  added_at    TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (user_id, track_id)
);

CREATE TABLE IF NOT EXISTS playlists (
  id          BIGSERIAL PRIMARY KEY,
  user_id     BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
  title       TEXT NOT NULL,
  created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS playlist_tracks (
  playlist_id BIGINT NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
  track_id    BIGINT NOT NULL,
  title       TEXT NOT NULL,
  artist      TEXT,
  duration    TEXT,
  position    INT DEFAULT 0,
  added_at    TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (playlist_id, track_id)
);

CREATE INDEX IF NOT EXISTS idx_library_user   ON library_tracks(user_id);
CREATE INDEX IF NOT EXISTS idx_fav_user       ON favorites(user_id);
CREATE INDEX IF NOT EXISTS idx_playlists_user ON playlists(user_id);
CREATE INDEX IF NOT EXISTS idx_invite_used    ON invite_codes(used_by);
"""

DURATIONS = {
    "7":       (7,    "7 дней"),
    "30":      (30,   "30 дней"),
    "90":      (90,   "90 дней"),
    "360":     (360,  "360 дней"),
    "forever": (None, "Навсегда"),
}

# ─── Helpers ───
def is_admin(tg_id: int) -> bool:
    return tg_id in ADMIN_IDS

def gen_key() -> str:
    h = secrets.token_hex(8).upper()
    return f"EVU-{h[0:4]}-{h[4:8]}-{h[8:12]}-{h[12:16]}"

def sub_is_active(row) -> bool:
    if not row:
        return False
    if row["subscription_type"] == "forever":
        return True
    until = row["subscription_until"]
    return bool(until and until > datetime.now(timezone.utc))

def sub_human(row) -> str:
    if not row or (row["subscription_type"] is None and row["subscription_until"] is None):
        return "нет"
    if row["subscription_type"] == "forever":
        return "💎 Навсегда"
    until = row["subscription_until"]
    if until and until > datetime.now(timezone.utc):
        return f"до {until.strftime('%d.%m.%Y %H:%M')}"
    return "истекла"

# ─── Keyboards ───
def webapp_kb(label: str = "Открыть EvuMusic") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, web_app=WebAppInfo(url=f"{WEBAPP_URL}/index.html"))
    ]])

def start_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Открыть EvuMusic", web_app=WebAppInfo(url=f"{WEBAPP_URL}/index.html"))],
        [InlineKeyboardButton(text="Канал с обновлениями", url="https://t.me/EvuMusic")],
    ])

def admin_keys_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="7 дней",  callback_data="genkey:7"),
         InlineKeyboardButton(text="30 дней", callback_data="genkey:30")],
        [InlineKeyboardButton(text="90 дней", callback_data="genkey:90"),
         InlineKeyboardButton(text="360 дней", callback_data="genkey:360")],
        [InlineKeyboardButton(text="💎 Навсегда", callback_data="genkey:forever")],
        [InlineKeyboardButton(text="✏️ Своя длительность", callback_data="genkey:custom")],
    ])

# ─── DB ops ───
async def upsert_user(user, invited_by: int | None = None) -> None:
    await pool.execute(
        """INSERT INTO users
             (telegram_id, username, first_name, last_name, language_code, is_admin, invited_by)
           VALUES ($1,$2,$3,$4,$5,$6,$7)
           ON CONFLICT (telegram_id) DO UPDATE SET
             username      = EXCLUDED.username,
             first_name    = EXCLUDED.first_name,
             last_name     = EXCLUDED.last_name,
             language_code = EXCLUDED.language_code,
             is_admin      = EXCLUDED.is_admin,
             last_seen_at  = now()""",
        user.id, user.username, user.first_name, user.last_name,
        user.language_code or "ru", is_admin(user.id), invited_by,
    )

async def get_user(tg_id: int):
    return await pool.fetchrow("SELECT * FROM users WHERE telegram_id=$1", tg_id)

async def is_banned(tg_id: int) -> bool:
    row = await pool.fetchrow("SELECT is_banned FROM users WHERE telegram_id=$1", tg_id)
    return bool(row and row["is_banned"])

async def use_invite_code(code: str, tg_id: int) -> dict:
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT created_by FROM invite_codes WHERE code=$1 AND used_by IS NULL FOR UPDATE",
                code,
            )
            if not row:
                return {"ok": False, "reason": "not_found"}
            r = await conn.execute(
                "UPDATE invite_codes SET used_by=$1, used_at=now() WHERE code=$2 AND used_by IS NULL",
                tg_id, code,
            )
            if r.endswith("0"):
                return {"ok": False, "reason": "already_used"}
            return {"ok": True, "inviter": row["created_by"]}

async def create_invite_code(tg_id: int) -> str:
    for _ in range(5):
        code = secrets.token_hex(4)
        try:
            await pool.execute(
                "INSERT INTO invite_codes (code, created_by) VALUES ($1,$2)",
                code, tg_id,
            )
            return code
        except asyncpg.UniqueViolationError:
            continue
    raise RuntimeError("could not generate unique invite code")

async def get_stats() -> dict:
    row = await pool.fetchrow("""
        SELECT
          (SELECT count(*) FROM users) AS users,
          (SELECT count(*) FROM users WHERE last_seen_at > now() - interval '7 days') AS active7d,
          (SELECT count(*) FROM invite_codes) AS invites_total,
          (SELECT count(*) FROM invite_codes WHERE used_by IS NOT NULL) AS invites_used,
          (SELECT count(*) FROM library_tracks) AS tracks,
          (SELECT count(*) FROM playlists) AS playlists,
          (SELECT count(*) FROM subscription_keys) AS keys_total,
          (SELECT count(*) FROM subscription_keys WHERE used_by IS NULL) AS keys_free,
          (SELECT count(*) FROM users WHERE subscription_type='forever') AS forever_users
    """)
    return dict(row)

async def generate_key(created_by: int, days: int | None, label: str) -> str:
    for _ in range(5):
        code = gen_key()
        try:
            await pool.execute(
                """INSERT INTO subscription_keys
                   (code, duration_days, duration_label, created_by)
                   VALUES ($1,$2,$3,$4)""",
                code, days, label, created_by,
            )
            return code
        except asyncpg.UniqueViolationError:
            continue
    raise RuntimeError("could not generate unique subscription key")

async def activate_key(code: str, tg_id: int) -> dict:
    async with pool.acquire() as conn:
        async with conn.transaction():
            user = await conn.fetchrow(
                "SELECT subscription_type FROM users WHERE telegram_id=$1", tg_id,
            )
            if user and user["subscription_type"] == "forever":
                return {"ok": False, "reason": "already_forever"}

            kr = await conn.fetchrow(
                "SELECT duration_days FROM subscription_keys WHERE code=$1 AND used_by IS NULL FOR UPDATE",
                code,
            )
            if not kr:
                exists = await conn.fetchval("SELECT 1 FROM subscription_keys WHERE code=$1", code)
                return {"ok": False, "reason": "used" if exists else "not_found"}

            days = kr["duration_days"]
            if days is None:
                await conn.execute(
                    "UPDATE users SET subscription_until=NULL, subscription_type='forever' WHERE telegram_id=$1",
                    tg_id,
                )
                sub_type, until = "forever", None
            else:
                sub_type = f"{days}d"
                until = await conn.fetchval(
                    """UPDATE users
                          SET subscription_until = GREATEST(COALESCE(subscription_until, now()), now())
                                                  + ($2 || ' days')::interval,
                              subscription_type  = $3
                        WHERE telegram_id = $1
                    RETURNING subscription_until""",
                    tg_id, days, sub_type,
                )

            await conn.execute(
                "UPDATE subscription_keys SET used_by=$1, used_at=now() WHERE code=$2",
                tg_id, code,
            )
            return {"ok": True, "until": until, "sub_type": sub_type, "days": days}

# ─── FSM / Filters / Middleware ───
class KeyGenStates(StatesGroup):
    waiting_custom_days = State()

class NotCommandText(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return bool(message.text and not message.text.startswith("/"))

class BanMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user:
            try:
                banned = await is_banned(user.id)
            except Exception:
                banned = False
            if banned:
                text = getattr(event, "text", "") or ""
                if not text.startswith(("/start", "/help")):
                    return None
        return await handler(event, data)

router = Router()
router.message.middleware(BanMiddleware())
router.callback_query.middleware(BanMiddleware())

# ─── Handlers ───
@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject):
    if await is_banned(message.from_user.id):
        await message.answer("⛔ Доступ заблокирован.")
        return

    payload = (command.args or "").strip()
    invited_by = None

    if payload.startswith("ref_"):
        try:
            invited_by = int(payload[4:])
        except ValueError:
            pass
    elif payload.startswith("inv_"):
        try:
            res = await use_invite_code(payload[4:], message.from_user.id)
            if res["ok"]:
                invited_by = res["inviter"]
        except Exception as e:
            log.exception("invite error: %s", e)

    await upsert_user(message.from_user, invited_by)
    await message.answer(
        "Твоя музыка, всегда рядом.\n\n"
        "Напиши название песни или открой плеер.\n\n"
        "Если у тебя есть ключ подписки — <code>/activate КОД</code>",
        reply_markup=start_kb(),
    )

@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "EvuMusic — закрытый музыкальный клуб.\n\n"
        "/start — открыть приложение\n"
        "/app — кнопка приложения\n"
        "/me — профиль\n"
        "/sub — статус подписки\n"
        "/activate КОД — активировать ключ\n"
        "/invite — инвайт (если есть доступ)\n"
        "/stats — статистика (админ)",
        reply_markup=webapp_kb(),
    )

@router.message(Command("app"))
async def cmd_app(message: Message):
    if await is_banned(message.from_user.id):
        return
    await message.answer("Открывай EvuMusic 👇", reply_markup=webapp_kb())

@router.message(Command("me"))
async def cmd_me(message: Message):
    row = await get_user(message.from_user.id)
    if not row:
        await message.answer("Сначала нажми /start", reply_markup=webapp_kb())
        return
    themes = {"blue": "Синяя", "white": "Белая", "black": "Чёрная"}
    uname = message.from_user.username
    name_line = f"@{uname}" if uname else escape(message.from_user.first_name or "—")
    invited = f"\nПригласил: <code>{row['invited_by']}</code>" if row["invited_by"] else ""
    role = "админ" if (is_admin(message.from_user.id) or row["is_admin"]) else "участник"
    await message.answer(
        f"Профиль: {name_line}\n"
        f"Тема: {themes.get(row['theme'], row['theme'])}\n"
        f"Роль: {role}\n"
        f"Подписка: {sub_human(row)}\n"
        f"С нами с: {row['created_at'].strftime('%d.%m.%Y')}"
        f"{invited}",
        reply_markup=webapp_kb(),
    )

@router.message(Command("sub", "subscription", "status"))
async def cmd_sub(message: Message):
    row = await get_user(message.from_user.id)
    if not row:
        await message.answer("Сначала нажми /start", reply_markup=webapp_kb())
        return
    if sub_is_active(row):
        await message.answer(
            f"✅ Подписка активна: <b>{sub_human(row)}</b>",
            reply_markup=webapp_kb(),
        )
    else:
        await message.answer(
            "❌ Активной подписки нет.\n\nАктивируй ключ: <code>/activate КОД</code>",
            reply_markup=webapp_kb(),
        )

@router.message(Command("activate"))
async def cmd_activate(message: Message, command: CommandObject):
    await upsert_user(message.from_user)
    code = (command.args or "").strip().upper()
    if not code:
        await message.answer("Использование: <code>/activate EVU-XXXX-XXXX-XXXX-XXXX</code>")
        return

    res = await activate_key(code, message.from_user.id)
    if not res["ok"]:
        msgs = {
            "not_found": "❌ Ключ не найден.",
            "used": "❌ Этот ключ уже был использован.",
            "already_forever": "💎 У тебя уже вечная подписка — ключ не потрачен.",
        }
        await message.answer(msgs.get(res["reason"], "❌ Не удалось активировать."))
        return

    if res["sub_type"] == "forever":
        await message.answer(
            "🎉 <b>Вечная подписка активирована!</b>",
            reply_markup=webapp_kb(),
        )
    else:
        await message.answer(
            f"✅ <b>Подписка продлена!</b>\n"
            f"Действует до {res['until'].strftime('%d.%m.%Y %H:%M')}",
            reply_markup=webapp_kb(),
        )

@router.message(Command("invite"))
async def cmd_invite(message: Message):
    row = await get_user(message.from_user.id)
    can_invite = bool(row and row["is_admin"]) or is_admin(message.from_user.id)
    if not can_invite:
        await message.answer("Инвайты выдаёт только администрация. Канал: https://t.me/EvuMusic")
        return
    code = await create_invite_code(message.from_user.id)
    me = await message.bot.get_me()
    link = f"https://t.me/{me.username}?start=inv_{code}"
    await message.answer(
        f"🎟 Инвайт-код: <code>{code}</code>\nСсылка: {link}",
        reply_markup=webapp_kb(),
    )

@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if not is_admin(message.from_user.id):
        return
    s = await get_stats()
    await message.answer(
        f"👥 Пользователей: {s['users']}\n"
        f"⚡ Активных (7д): {s['active7d']}\n"
        f"🎟 Инвайтов: {s['invites_used']}/{s['invites_total']}\n"
        f"🎵 Треков: {s['tracks']}\n"
        f"📼 Плейлистов: {s['playlists']}\n"
        f"🔑 Ключей всего: {s['keys_total']} (свободных: {s['keys_free']})\n"
        f"💎 Вечных подписок: {s['forever_users']}"
    )

# ─── Admin: keys ───
@router.message(Command("key"))
async def cmd_key(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "🔑 <b>Генерация ключа подписки</b>\n\nВыбери длительность:",
        reply_markup=admin_keys_kb(),
    )

@router.message(Command("genkey"))
async def cmd_genkey(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    arg = (command.args or "").strip().lower()
    if arg not in DURATIONS:
        await message.answer("Использование: <code>/genkey 7|30|90|360|forever</code>")
        return
    days, label = DURATIONS[arg]
    code = await generate_key(message.from_user.id, days, label)
    await message.answer(
        f"✅ Ключ на <b>{label}</b>:\n\n"
        f"<code>{code}</code>\n\n"
        f"Активация: <code>/activate {code}</code>"
    )

@router.message(Command("keys"))
async def cmd_keys(message: Message):
    if not is_admin(message.from_user.id):
        return
    rows = await pool.fetch(
        "SELECT code, duration_days, used_by FROM subscription_keys "
        "ORDER BY created_at DESC LIMIT 20"
    )
    if not rows:
        await message.answer("Ключей пока нет. Используй /key.")
        return
    lines = ["Последние 20 ключей:\n"]
    for r in rows:
        dur = "навсегда" if r["duration_days"] is None else f"{r['duration_days']}д"
        status = "✅" if r["used_by"] else "🆓"
        lines.append(f"{status} <code>{r['code']}</code> — {dur}")
    await message.answer("\n".join(lines))

@router.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Использование: <code>/ban &lt;telegram_id&gt;</code>")
        return
    await pool.execute("UPDATE users SET is_banned=TRUE WHERE telegram_id=$1", int(arg))
    await message.answer(f"⛔ Пользователь <code>{arg}</code> забанен.")

@router.message(Command("unban"))
async def cmd_unban(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Использование: <code>/unban &lt;telegram_id&gt;</code>")
        return
    await pool.execute("UPDATE users SET is_banned=FALSE WHERE telegram_id=$1", int(arg))
    await message.answer(f"✅ Пользователь <code>{arg}</code> разбанен.")

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "🛠 <b>Админ-панель</b>\n\n"
        "/key — меню генерации ключей\n"
        "/genkey &lt;7|30|90|360|forever&gt; — быстрая генерация\n"
        "/keys — последние 20 ключей\n"
        "/stats — статистика\n"
        "/ban &lt;id&gt; / /unban &lt;id&gt;\n"
        "/invite — создать инвайт-код"
    )

@router.callback_query(F.data.startswith("genkey:"))
async def cb_genkey(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только для админов", show_alert=True)
        return
    arg = callback.data.split(":", 1)[1]
    if arg == "custom":
        await callback.message.answer("✏️ Введи количество дней (1–36500):")
        await state.set_state(KeyGenStates.waiting_custom_days)
        await callback.answer()
        return
    if arg not in DURATIONS:
        await callback.answer("Неизвестная длительность", show_alert=True)
        return
    days, label = DURATIONS[arg]
    code = await generate_key(callback.from_user.id, days, label)
    await callback.message.answer(
        f"✅ Ключ на <b>{label}</b>:\n\n"
        f"<code>{code}</code>\n\n"
        f"Активация: <code>/activate {code}</code>"
    )
    await callback.answer("Ключ создан")

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.")

@router.message(KeyGenStates.waiting_custom_days, NotCommandText())
async def process_custom_days(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    try:
        days = int((message.text or "").strip())
        if not 1 <= days <= 36500:
            raise ValueError
    except (ValueError, TypeError):
        await message.answer("Нужно целое число 1–36500. Попробуй ещё раз или /cancel.")
        return
    await state.clear()
    label = f"{days} дней"
    code = await generate_key(message.from_user.id, days, label)
    await message.answer(
        f"✅ Ключ на <b>{label}</b>:\n\n"
        f"<code>{code}</code>\n\n"
        f"Активация: <code>/activate {code}</code>"
    )

@router.message(NotCommandText())
async def any_text(message: Message):
    if await is_banned(message.from_user.id):
        return
    await upsert_user(message.from_user)
    await message.answer(
        "Поиск треков — в приложении 👇",
        reply_markup=webapp_kb("Открыть поиск"),
    )

# ─── Entrypoint ───
async def main() -> None:
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    async with pool.acquire() as conn:
        await conn.execute(SQL_SCHEMA)
    log.info("DB schema OK")

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    try:
        await bot.set_my_commands([
            BotCommand(command="start",    description="Открыть EvuMusic"),
            BotCommand(command="app",      description="Кнопка приложения"),
            BotCommand(command="me",       description="Мой профиль"),
            BotCommand(command="sub",      description="Моя подписка"),
            BotCommand(command="activate", description="Активировать ключ"),
            BotCommand(command="invite",   description="Создать инвайт"),
            BotCommand(command="help",     description="Помощь"),
        ])
    except Exception as e:
        log.warning("set_my_commands failed: %s", e)

    log.info("EvuMusic bot starting...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
