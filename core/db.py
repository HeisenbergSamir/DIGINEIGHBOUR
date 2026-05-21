"""DigiNeighbour — Database Layer"""
import sqlite3, json, os

DB_PATH = os.environ.get("DN_DB", "/app/data/dn.db")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS communities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    slug TEXT UNIQUE NOT NULL,
    bot_token TEXT,
    bot_username TEXT,
    description TEXT,
    welcome_msg TEXT DEFAULT 'Welcome! Use the menu to get started.',
    is_active INTEGER DEFAULT 1,
    require_invite INTEGER DEFAULT 1,
    max_invites_per_user INTEGER DEFAULT 10,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS invite_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    community_id INTEGER REFERENCES communities(id) ON DELETE CASCADE,
    code TEXT UNIQUE NOT NULL,
    created_by INTEGER REFERENCES users(id),
    used_by INTEGER REFERENCES users(id),
    is_used INTEGER DEFAULT 0,
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now')),
    used_at TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    community_id INTEGER REFERENCES communities(id) ON DELETE CASCADE,
    telegram_id INTEGER NOT NULL,
    telegram_username TEXT,
    display_name TEXT,
    flat_number TEXT,
    phone TEXT,
    role TEXT DEFAULT 'member',
    is_verified INTEGER DEFAULT 0,
    is_banned INTEGER DEFAULT 0,
    ban_reason TEXT,
    invited_by INTEGER REFERENCES users(id),
    invite_code_used TEXT,
    invites_generated INTEGER DEFAULT 0,
    joined_at TEXT DEFAULT (datetime('now')),
    last_active TEXT,
    UNIQUE(community_id, telegram_id)
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    community_id INTEGER REFERENCES communities(id) ON DELETE CASCADE,
    parent_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    slug TEXT NOT NULL,
    command TEXT,
    icon TEXT DEFAULT '📌',
    description TEXT,
    sort_order INTEGER DEFAULT 0,
    is_active INTEGER DEFAULT 1,
    requires_approval INTEGER DEFAULT 0,
    listing_days INTEGER DEFAULT 60,
    max_listings_per_user_daily INTEGER DEFAULT 0,
    max_listings_per_user_weekly INTEGER DEFAULT 0,
    max_listings_per_user_monthly INTEGER DEFAULT 0,
    max_listings_per_user_total INTEGER DEFAULT 0,
    schema_fields TEXT DEFAULT '[]',
    start_date TEXT,
    end_date TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(community_id, slug)
);

CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    community_id INTEGER REFERENCES communities(id) ON DELETE CASCADE,
    category_id INTEGER REFERENCES categories(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    data TEXT DEFAULT '{}',
    status TEXT DEFAULT 'active',
    expires_at TEXT,
    view_count INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE VIRTUAL TABLE IF NOT EXISTS listings_fts USING fts5(
    title, data, content=listings, content_rowid=id
);
CREATE TRIGGER IF NOT EXISTS l_ai AFTER INSERT ON listings BEGIN
    INSERT INTO listings_fts(rowid,title,data) VALUES(new.id,new.title,new.data);
END;
CREATE TRIGGER IF NOT EXISTS l_ad AFTER DELETE ON listings BEGIN
    INSERT INTO listings_fts(listings_fts,rowid,title,data) VALUES('delete',old.id,old.title,old.data);
END;
CREATE TRIGGER IF NOT EXISTS l_au AFTER UPDATE ON listings BEGIN
    INSERT INTO listings_fts(listings_fts,rowid,title,data) VALUES('delete',old.id,old.title,old.data);
    INSERT INTO listings_fts(rowid,title,data) VALUES(new.id,new.title,new.data);
END;

CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    category_id INTEGER REFERENCES categories(id) ON DELETE CASCADE,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(user_id, category_id)
);

CREATE TABLE IF NOT EXISTS conversations (
    telegram_id INTEGER NOT NULL,
    community_id INTEGER NOT NULL,
    state TEXT DEFAULT 'idle',
    context TEXT DEFAULT '{}',
    updated_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY(telegram_id, community_id)
);

CREATE TABLE IF NOT EXISTS group_buys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    community_id INTEGER REFERENCES communities(id),
    category_id INTEGER REFERENCES categories(id),
    created_by INTEGER REFERENCES users(id),
    title TEXT NOT NULL,
    description TEXT,
    item_url TEXT,
    ticket_price REAL DEFAULT 0,
    threshold_amount REAL DEFAULT 0,
    advance_percent REAL DEFAULT 20,
    max_tickets_per_user INTEGER DEFAULT 10,
    reward_percent REAL DEFAULT 0.5,
    status TEXT DEFAULT 'open',
    expires_at TEXT,
    telegram_group_link TEXT,
    winner_user_id INTEGER REFERENCES users(id),
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS group_buy_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_buy_id INTEGER REFERENCES group_buys(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id),
    tickets INTEGER DEFAULT 1,
    advance_paid INTEGER DEFAULT 0,
    advance_amount REAL DEFAULT 0,
    forfeited INTEGER DEFAULT 0,
    quote_text TEXT,
    quote_amount REAL,
    votes_received INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(group_buy_id, user_id)
);

CREATE TABLE IF NOT EXISTS votes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_buy_id INTEGER REFERENCES group_buys(id),
    voter_id INTEGER REFERENCES users(id),
    entry_id INTEGER REFERENCES group_buy_entries(id),
    voted_at TEXT DEFAULT (datetime('now')),
    UNIQUE(group_buy_id, voter_id)
);

CREATE TABLE IF NOT EXISTS sponsorships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    community_id INTEGER REFERENCES communities(id) ON DELETE CASCADE,
    sponsor_name TEXT NOT NULL,
    banner_text TEXT,
    description TEXT,
    link TEXT,
    image_url TEXT,
    shows_per_day INTEGER DEFAULT 3,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    is_active INTEGER DEFAULT 1,
    show_count INTEGER DEFAULT 0,
    today_show_count INTEGER DEFAULT 0,
    last_shown_date TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sponsorship_inquiries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    community_id INTEGER REFERENCES communities(id),
    telegram_id INTEGER,
    telegram_username TEXT,
    name TEXT,
    business TEXT,
    message TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS approval_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id INTEGER REFERENCES listings(id),
    submitted_by INTEGER REFERENCES users(id),
    status TEXT DEFAULT 'pending',
    note TEXT,
    reviewed_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER,
    action TEXT NOT NULL,
    details TEXT,
    ip TEXT,
    at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    token TEXT PRIMARY KEY,
    admin_id INTEGER,
    role TEXT,
    expires_at TEXT
);

CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    display_name TEXT,
    password_hash TEXT NOT NULL,
    role TEXT DEFAULT 'admin',
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_l_cat    ON listings(category_id);
CREATE INDEX IF NOT EXISTS idx_l_user   ON listings(user_id);
CREATE INDEX IF NOT EXISTS idx_l_status ON listings(status);
CREATE INDEX IF NOT EXISTS idx_l_exp    ON listings(expires_at);
CREATE INDEX IF NOT EXISTS idx_u_tg     ON users(telegram_id, community_id);
CREATE INDEX IF NOT EXISTS idx_sub_u    ON subscriptions(user_id);
CREATE INDEX IF NOT EXISTS idx_sub_c    ON subscriptions(category_id);
CREATE INDEX IF NOT EXISTS idx_inv_code ON invite_links(code);
CREATE INDEX IF NOT EXISTS idx_sp_comm  ON sponsorships(community_id, is_active);
"""

def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    print(f"[DB] Ready: {DB_PATH}")

def q(sql, params=(), one=False):
    with get_db() as conn:
        cur = conn.execute(sql, params)
        rows = cur.fetchall()
        return (dict(rows[0]) if rows else None) if one else [dict(r) for r in rows]

def run(sql, params=()):
    with get_db() as conn:
        cur = conn.execute(sql, params)
        return cur.lastrowid

def get_setting(key, default=None):
    try:
        r = q("SELECT value FROM settings WHERE key=?", (key,), one=True)
        return r["value"] if r else default
    except Exception:
        return default

def set_setting(key, value):
    run("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')", (key, str(value)))
