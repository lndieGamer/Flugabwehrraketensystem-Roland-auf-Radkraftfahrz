"""Слой БД: схема и все запросы. Агрегаты не материализуются — объём позволяет."""
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import aiosqlite
from dotenv import load_dotenv

load_dotenv()
DB_PATH = os.getenv('DB_PATH', 'data/stats.db')
TZ = ZoneInfo(os.getenv('BOT_TZ', 'Europe/Moscow'))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    vk_id         INTEGER PRIMARY KEY,
    display_name  TEXT NOT NULL,
    is_community  INTEGER DEFAULT 0,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    cmid            INTEGER NOT NULL UNIQUE,
    vk_id           INTEGER NOT NULL REFERENCES users(vk_id),
    ts              TEXT NOT NULL,
    text            TEXT,
    word_count      INTEGER DEFAULT 0,
    is_reply        INTEGER DEFAULT 0,
    reply_to_cmid   INTEGER,
    has_sticker     INTEGER DEFAULT 0,
    has_photo       INTEGER DEFAULT 0,
    has_attachment  INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_messages_vkid_ts ON messages(vk_id, ts);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);

CREATE TABLE IF NOT EXISTS milestones_sent (
    milestone_type  TEXT NOT NULL,
    milestone_value TEXT NOT NULL,
    sent_at         TEXT NOT NULL,
    PRIMARY KEY (milestone_type, milestone_value)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now_local() -> datetime:
    """Текущее время в часовом поясе чата, naive — как хранится в БД."""
    return datetime.now(TZ).replace(tzinfo=None)


async def connect(db_path: str | None = None) -> aiosqlite.Connection:
    path = db_path or DB_PATH
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    db = await aiosqlite.connect(path)
    # WAL + busy_timeout: бот и импортёр могут работать одновременно без «database is locked»
    await db.execute('PRAGMA journal_mode=WAL')
    await db.execute('PRAGMA busy_timeout=5000')
    await db.executescript(SCHEMA)
    await db.commit()
    return db


async def upsert_user(db, vk_id: int, display_name: str, is_community: int, ts: str):
    await db.execute(
        """INSERT INTO users (vk_id, display_name, is_community, first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(vk_id) DO UPDATE SET
             display_name = excluded.display_name,
             is_community = max(is_community, excluded.is_community),
             first_seen   = min(first_seen, excluded.first_seen),
             last_seen    = max(last_seen, excluded.last_seen)""",
        (vk_id, display_name, is_community, ts, ts),
    )


async def insert_message(db, msg: dict) -> bool:
    """INSERT OR IGNORE по UNIQUE(cmid). True — вставлено, False — дубликат."""
    cur = await db.execute(
        """INSERT OR IGNORE INTO messages
           (cmid, vk_id, ts, text, word_count, is_reply, reply_to_cmid,
            has_sticker, has_photo, has_attachment)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (msg['cmid'], msg['vk_id'], msg['ts'], msg['text'], msg['word_count'],
         msg['is_reply'], msg['reply_to_cmid'], msg['has_sticker'],
         msg['has_photo'], msg['has_attachment']),
    )
    return cur.rowcount == 1


async def get_total_count(db) -> int:
    async with db.execute('SELECT COUNT(*) FROM messages') as cur:
        return (await cur.fetchone())[0]


async def get_user_count(db, vk_id: int) -> int:
    async with db.execute('SELECT COUNT(*) FROM messages WHERE vk_id = ?', (vk_id,)) as cur:
        return (await cur.fetchone())[0]


async def get_display_name(db, vk_id: int) -> str | None:
    async with db.execute('SELECT display_name FROM users WHERE vk_id = ?', (vk_id,)) as cur:
        row = await cur.fetchone()
        return row[0] if row else None


async def get_first_message_ts(db) -> str | None:
    async with db.execute('SELECT MIN(ts) FROM messages') as cur:
        return (await cur.fetchone())[0]


def _range_where(vk_id=None, since=None, until=None, col='ts'):
    """WHERE-фрагмент для необязательных фильтров по автору и периоду."""
    conds, params = [], []
    if vk_id is not None:
        conds.append('vk_id = ?')
        params.append(vk_id)
    if since:
        conds.append(f'{col} >= ?')
        params.append(since)
    if until:
        conds.append(f'{col} <= ?')
        params.append(until)
    return (('WHERE ' + ' AND '.join(conds)) if conds else ''), params


async def get_human_messages(db, since: str | None = None,
                             until: str | None = None) -> list[tuple[int, str]]:
    """[(vk_id, ts)] всех сообщений людей (сообщества исключены) по возрастанию ts."""
    where, params = _range_where(since=since, until=until, col='m.ts')
    extra = where.replace('WHERE', 'AND') if where else ''
    async with db.execute(
        f"""SELECT m.vk_id, m.ts FROM messages m JOIN users u ON u.vk_id = m.vk_id
            WHERE u.is_community = 0 {extra} ORDER BY m.ts""", params
    ) as cur:
        return await cur.fetchall()


async def get_all_names(db) -> dict[int, str]:
    async with db.execute('SELECT vk_id, display_name FROM users') as cur:
        return dict(await cur.fetchall())


async def get_timestamps(db, vk_id: int | None = None, since: str | None = None,
                         until: str | None = None) -> list[str]:
    """ts (ISO) пользователя или всего чата за период — для построения графика."""
    where, params = _range_where(vk_id, since, until)
    async with db.execute(
        f'SELECT ts FROM messages {where} ORDER BY ts', params
    ) as cur:
        return [r[0] for r in await cur.fetchall()]


async def get_activity_stats(db, vk_id: int | None = None, today: date | None = None,
                             since: str | None = None, until: str | None = None) -> dict | None:
    """Сова (0–6) / жаворонок (6–12), streak, средняя длина. vk_id=None — весь чат.
    При заданном until streak считается относительно конца периода."""
    where, params = _range_where(vk_id, since, until)
    if today is None and until:
        today = date.fromisoformat(until[:10])
    async with db.execute(
        f"""SELECT COUNT(*), AVG(word_count),
                   AVG(substr(ts, 12, 2) < '06'),
                   AVG(substr(ts, 12, 2) >= '06' AND substr(ts, 12, 2) < '12')
            FROM messages {where}""", params
    ) as cur:
        total, avg_words, night, morning = await cur.fetchone()
    if not total:
        return None

    async with db.execute(
        f'SELECT DISTINCT substr(ts, 1, 10) FROM messages {where}', params
    ) as cur:
        days = {date.fromisoformat(r[0]) for r in await cur.fetchall()}
    today = today or now_local().date()
    cur_day = today if today in days else today - timedelta(days=1)
    streak = 0
    while cur_day in days:
        streak += 1
        cur_day -= timedelta(days=1)

    return {
        'total': total,
        'avg_words': avg_words or 0.0,
        'night_share': night or 0.0,
        'morning_share': morning or 0.0,
        'streak': streak,
    }


async def get_setting(db, key: str) -> str | None:
    async with db.execute('SELECT value FROM settings WHERE key = ?', (key,)) as cur:
        row = await cur.fetchone()
        return row[0] if row else None


async def set_setting(db, key: str, value: str):
    await db.execute('INSERT OR REPLACE INTO settings VALUES (?, ?)', (key, value))
    await db.commit()


async def milestone_was_sent(db, mtype: str, value: str) -> bool:
    async with db.execute(
        'SELECT 1 FROM milestones_sent WHERE milestone_type = ? AND milestone_value = ?',
        (mtype, value),
    ) as cur:
        return await cur.fetchone() is not None


async def mark_milestone_sent(db, mtype: str, value: str):
    await db.execute(
        'INSERT OR IGNORE INTO milestones_sent VALUES (?, ?, ?)',
        (mtype, value, now_local().isoformat(timespec='seconds')),
    )
    await db.commit()
