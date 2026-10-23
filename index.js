import 'dotenv/config';
import { Bot, InlineKeyboard } from 'grammy';
import pg from 'pg';
import crypto from 'node:crypto';

const { Pool } = pg;

const BOT_TOKEN = process.env.BOT_TOKEN;
const WEBAPP_URL = (process.env.WEBAPP_URL || '').replace(/\/+$/, '');
const ADMIN_IDS = (process.env.ADMIN_IDS || '')
  .split(',')
  .map((s) => s.trim())
  .filter(Boolean)
  .map(Number);

if (!BOT_TOKEN) throw new Error('BOT_TOKEN is not set');
if (!WEBAPP_URL) throw new Error('WEBAPP_URL is not set');

const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
  ssl: { rejectUnauthorized: false },
});

const bot = new Bot(BOT_TOKEN);

/* ---------- DB helpers ---------- */

async function upsertUser(user, invitedBy = null) {
  const isAdmin = ADMIN_IDS.includes(user.id);
  await pool.query(
    `INSERT INTO users (telegram_id, username, first_name, last_name, language_code, is_admin, invited_by)
     VALUES ($1, $2, $3, $4, $5, $6, $7)
     ON CONFLICT (telegram_id) DO UPDATE SET
       username     = EXCLUDED.username,
       first_name   = EXCLUDED.first_name,
       last_name    = EXCLUDED.last_name,
       language_code= EXCLUDED.language_code,
       is_admin     = EXCLUDED.is_admin,
       last_seen_at = now()`,
    [
      user.id,
      user.username || null,
      user.first_name || null,
      user.last_name || null,
      user.language_code || 'ru',
      isAdmin,
      invitedBy,
    ]
  );
}

async function isBanned(telegramId) {
  const { rows } = await pool.query(
    'SELECT is_banned FROM users WHERE telegram_id = $1',
    [telegramId]
  );
  return rows[0]?.is_banned === true;
}

async function useInviteCode(code, telegramId) {
  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    const { rows } = await client.query(
      `SELECT code, created_by FROM invite_codes
       WHERE code = $1 AND used_by IS NULL FOR UPDATE`,
      [code]
    );
    if (!rows.length) {
      await client.query('ROLLBACK');
      return { ok: false, reason: 'not_found' };
    }
    const inviter = rows[0].created_by;
    const upd = await client.query(
      `UPDATE invite_codes
       SET used_by = $1, used_at = now()
       WHERE code = $2 AND used_by IS NULL
       RETURNING code`,
      [telegramId, code]
    );
    if (!upd.rowCount) {
      await client.query('ROLLBACK');
      return { ok: false, reason: 'already_used' };
    }
    await client.query('COMMIT');
    return { ok: true, inviter };
  } catch (e) {
    await client.query('ROLLBACK');
    throw e;
  } finally {
    client.release();
  }
}

async function createInviteCode(telegramId) {
  const code = crypto.randomBytes(4).toString('hex');
  await pool.query(
    `INSERT INTO invite_codes (code, created_by) VALUES ($1, $2)`,
    [code, telegramId]
  );
  return code;
}

async function stats() {
  const { rows } = await pool.query(`
    SELECT
      (SELECT count(*) FROM users) AS users,
      (SELECT count(*) FROM users WHERE last_seen_at > now() - interval '7 days') AS active7d,
      (SELECT count(*) FROM invite_codes) AS invites_total,
      (SELECT count(*) FROM invite_codes WHERE used_by IS NOT NULL) AS invites_used,
      (SELECT count(*) FROM library_tracks) AS tracks,
      (SELECT count(*) FROM playlists) AS playlists
  `);
  return rows[0];
}

function appButton(label = 'Открыть EvuMusic') {
  return InlineKeyboard.webApp(label, `${WEBAPP_URL}/index.html`);
}

function startKeyboard() {
  return new InlineKeyboard()
    .webApp('Открыть EvuMusic', `${WEBAPP_URL}/index.html`)
    .url('Канал с обновлениями', 'https://t.me/EvuMusic');
}

/* ---------- Middleware ---------- */

bot.command('start', async (ctx) => {
  const payload = ctx.match || '';
  let invitedBy = null;

  if (payload.startsWith('ref_')) {
    const inviterId = Number(payload.slice(4));
    if (Number.isFinite(inviterId)) invitedBy = inviterId;
  }

  if (payload.startsWith('inv_')) {
    const code = payload.slice(4);
    try {
      const res = await useInviteCode(code, ctx.from.id);
      if (res.ok) {
        invitedBy = res.inviter;
      }
    } catch (e) {
      console.error('invite error', e);
    }
  }

  await upsertUser(ctx.from, invitedBy);

  await ctx.reply(
    'Твоя музыка, всегда рядом.\n\n' +
      'Напиши название песни или открой плеер: поиск, любимые треки и личные плейлисты.',
    { reply_markup: startKeyboard() }
  );
});

bot.command('help', async (ctx) => {
  await ctx.reply(
    'EvuMusic — закрытый музыкальный клуб.\n\n' +
      '/start — открыть приложение\n' +
      '/app — кнопка приложения\n' +
      '/invite — создать инвайт-код (если есть доступ)\n' +
      '/me — твой профиль\n' +
      '/stats — статистика (админ)',
    { reply_markup: appButton() }
  );
});

bot.command('app', async (ctx) => {
  if (await isBanned(ctx.from.id)) return;
  await ctx.reply('Открывай EvuMusic 👇', { reply_markup: appButton() });
});

bot.command('me', async (ctx) => {
  const { rows } = await pool.query(
    `SELECT theme, created_at, invited_by, is_admin FROM users WHERE telegram_id = $1`,
    [ctx.from.id]
  );
  if (!rows.length) {
    await ctx.reply('Сначала нажми /start', { reply_markup: appButton() });
    return;
  }
  const u = rows[0];
  const themes = { blue: 'Синяя', white: 'Белая', black: 'Чёрная' };
  await ctx.reply(
    `Профиль: @${ctx.from.username || ctx.from.first_name}\n` +
      `Тема: ${themes[u.theme] || u.theme}\n` +
      `Роль: ${u.is_admin ? 'админ' : 'участник'}\n` +
      `С нами с: ${new Date(u.created_at).toLocaleDateString('ru-RU')}` +
      (u.invited_by ? `\nПригласил: ${u.invited_by}` : ''),
    { reply_markup: appButton() }
  );
});

bot.command('invite', async (ctx) => {
  const userId = ctx.from.id;
  const { rows } = await pool.query(
    'SELECT is_admin FROM users WHERE telegram_id = $1',
    [userId]
  );
  const canInvite = rows[0]?.is_admin || ADMIN_IDS.includes(userId);
  if (!canInvite) {
    await ctx.reply('Инвайты выдаёт только администрация. Пиши в канал: https://t.me/EvuMusic');
    return;
  }
  const code = await createInviteCode(userId);
  const link = `https://t.me/${(await bot.api.getMe()).username}?start=inv_${code}`;
  await ctx.reply(
    `Инвайт-код: <code>${code}</code>\nСсылка: ${link}`,
    { parse_mode: 'HTML', reply_markup: appButton() }
  );
});

bot.command('stats', async (ctx) => {
  if (!ADMIN_IDS.includes(ctx.from.id)) return;
  const s = await stats();
  await ctx.reply(
    `👥 Пользователей: ${s.users}\n` +
      `⚡ Активных (7д): ${s.active7d}\n` +
      `🎟 Инвайтов: ${s.invites_used}/${s.invites_total}\n` +
      `🎵 Треков в библиотеках: ${s.tracks}\n` +
      `📼 Плейлистов: ${s.playlists}`
  );
});

/* Любое текстовое сообщение — подсказка открыть плеер */
bot.on('message:text', async (ctx) => {
  if (ctx.message.text.startsWith('/')) return;
  if (await isBanned(ctx.from.id)) return;
  await upsertUser(ctx.from);
  await ctx.reply(
    'Поиск треков — в приложении 👇',
    { reply_markup: appButton('Открыть поиск') }
  );
});

/* Ошибки */
bot.catch((err) => {
  console.error('Bot error:', err.error);
});

/* Start */
bot.api.setMyCommands([
  { command: 'start', description: 'Открыть EvuMusic' },
  { command: 'app', description: 'Кнопка приложения' },
  { command: 'invite', description: 'Создать инвайт-код' },
  { command: 'me', description: 'Мой профиль' },
  { command: 'help', description: 'Помощь' },
  { command: 'stats', description: 'Статистика (админ)' },
]);

console.log('EvuMusic bot starting...');
bot.start({
  onStart: (me) => console.log(`Bot @${me.username} online. WebApp: ${WEBAPP_URL}`),
});
