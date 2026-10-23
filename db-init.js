import 'dotenv/config';
import pg from 'pg';

const { Pool } = pg;

const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
  ssl: { rejectUnauthorized: false },
});

const SQL = `
CREATE TABLE IF NOT EXISTS users (
  id            BIGSERIAL PRIMARY KEY,
  telegram_id   BIGINT UNIQUE NOT NULL,
  username      TEXT,
  first_name    TEXT,
  last_name     TEXT,
  language_code TEXT DEFAULT 'ru',
  theme         TEXT DEFAULT 'blue' CHECK (theme IN ('blue','white','black')),
  is_admin      BOOLEAN DEFAULT FALSE,
  is_banned     BOOLEAN DEFAULT FALSE,
  invited_by    BIGINT REFERENCES users(telegram_id),
  created_at    TIMESTAMPTZ DEFAULT now(),
  last_seen_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS invite_codes (
  code        TEXT PRIMARY KEY,
  created_by  BIGINT NOT NULL REFERENCES users(telegram_id),
  used_by     BIGINT UNIQUE REFERENCES users(telegram_id),
  created_at  TIMESTAMPTZ DEFAULT now(),
  used_at     TIMESTAMPTZ
);

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

CREATE INDEX IF NOT EXISTS idx_library_user ON library_tracks(user_id);
CREATE INDEX IF NOT EXISTS idx_fav_user ON favorites(user_id);
CREATE INDEX IF NOT EXISTS idx_playlists_user ON playlists(user_id);
CREATE INDEX IF NOT EXISTS idx_invite_used ON invite_codes(used_by);
`;

async function main() {
  await pool.query(SQL);
  console.log('Neon schema OK');
  await pool.end();
}

main().catch((err) => {
  console.error('DB init failed:', err.message);
  process.exit(1);
});
