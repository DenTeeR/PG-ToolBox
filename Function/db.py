"""Local configuration and conversation state; never mixes panel and bot users."""

import json
import os
import sqlite3
from functools import lru_cache
from contextlib import contextmanager
from pathlib import Path

from Function.api import PasarGuardClient


def db_path():
    return Path(os.environ.get("PG_DB", Path(__file__).resolve().parents[1] / "holder.db"))


@contextmanager
def connect():
    conn = sqlite3.connect(db_path(), timeout=20)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def initialize():
    with connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS bot (chatid INTEGER PRIMARY KEY, token TEXT);
        CREATE TABLE IF NOT EXISTS users (chatid INTEGER PRIMARY KEY, role TEXT, name TEXT,
            username TEXT, password TEXT, domain TEXT, step TEXT, api_key TEXT DEFAULT '', backend TEXT DEFAULT 'unconfigured');
        CREATE TABLE IF NOT EXISTS monitoring (chatid INTEGER PRIMARY KEY, status TEXT,
            check_normal INTEGER, check_error INTEGER);
        CREATE TABLE IF NOT EXISTS messages (chatid INTEGER PRIMARY KEY, status TEXT);
        CREATE TABLE IF NOT EXISTS pg_templates (name TEXT PRIMARY KEY, data TEXT,
            days INTEGER, group_ids TEXT);
        CREATE TABLE IF NOT EXISTS alert_state (chatid INTEGER, user_id INTEGER, status TEXT,
            PRIMARY KEY (chatid, user_id));
        """)
        if "api_key" not in {r[1] for r in conn.execute("PRAGMA table_info(users)")}:
            conn.execute("ALTER TABLE users ADD COLUMN api_key TEXT DEFAULT ''")
        if "backend" not in {r[1] for r in conn.execute("PRAGMA table_info(users)")}:
            conn.execute("ALTER TABLE users ADD COLUMN backend TEXT DEFAULT 'unconfigured'")
        if conn.execute("PRAGMA user_version").fetchone()[0] < 1:
            conn.execute("UPDATE users SET step='None'")
            # Old notice switches enabled destructive inbound rewriting. New
            # alerts require a fresh baseline and explicit activation in the UI.
            conn.execute("UPDATE messages SET status='off'")
            conn.execute("PRAGMA user_version=1")
    os.chmod(db_path(), 0o600)


def config(chatid=None):
    with connect() as conn:
        if chatid is None:
            row = conn.execute("SELECT * FROM users WHERE role='boss' LIMIT 1").fetchone()
        else:
            row = conn.execute("SELECT * FROM users WHERE chatid=? AND role='boss'", (chatid,)).fetchone()
        if not row or row["backend"] != "pasarguard":
            raise ValueError("Run setup.py to configure PG-ToolBox for PasarGuard.")
        return dict(row)


def authorized(chatid):
    with connect() as conn:
        return (
            conn.execute(
                "SELECT 1 FROM users WHERE chatid=? AND role='boss' AND backend='pasarguard'", (chatid,)
            ).fetchone()
            is not None
        )


@lru_cache(maxsize=16)
def _client(url, user, password, key):
    return PasarGuardClient(url, user, password, key)


def client_for(chatid=None):
    info = config(chatid)
    return _client(info["domain"], info["username"], info["password"], info["api_key"] or "")


def bot_token():
    with connect() as conn:
        row = conn.execute("SELECT token FROM bot LIMIT 1").fetchone()
        if not row:
            raise ValueError("Run setup.py first.")
        return row[0]


def state(chatid):
    with connect() as conn:
        row = conn.execute("SELECT step FROM users WHERE chatid=?", (chatid,)).fetchone()
    try:
        value = json.loads(row[0]) if row else {}
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def set_state(chatid, value=None):
    with connect() as conn:
        conn.execute("UPDATE users SET step=? WHERE chatid=?", (json.dumps(value or {}), chatid))


def templates():
    with connect() as conn:
        rows = conn.execute("SELECT * FROM pg_templates ORDER BY name").fetchall()
    return [{**dict(row), "group_ids": json.loads(row["group_ids"])} for row in rows]


def save_template(name, gb, days, groups):
    with connect() as conn:
        conn.execute("INSERT INTO pg_templates VALUES (?, ?, ?, ?)", (name, gb, days, json.dumps(groups)))


def delete_template(name):
    with connect() as conn:
        conn.execute("DELETE FROM pg_templates WHERE name=?", (name,))


def monitoring(chatid):
    with connect() as conn:
        return dict(conn.execute("SELECT * FROM monitoring WHERE chatid=?", (chatid,)).fetchone())


def set_monitoring(chatid, **fields):
    if not fields or not set(fields) <= {"status", "check_normal", "check_error"}:
        raise ValueError("Invalid monitoring setting.")
    with connect() as conn:
        for key, value in fields.items():
            conn.execute(f"UPDATE monitoring SET {key}=? WHERE chatid=?", (value, chatid))


def alerts_enabled(chatid):
    with connect() as conn:
        row = conn.execute("SELECT status FROM messages WHERE chatid=?", (chatid,)).fetchone()
        return bool(row and row[0] == "on")


def toggle_alerts(chatid):
    enabled = alerts_enabled(chatid)
    users = client_for(chatid).users() if not enabled else []
    with connect() as conn:
        if not enabled:
            conn.execute("DELETE FROM alert_state WHERE chatid=?", (chatid,))
            conn.executemany(
                "INSERT INTO alert_state VALUES (?, ?, ?)", [(chatid, user["id"], user["status"]) for user in users]
            )
        conn.execute("UPDATE messages SET status=? WHERE chatid=?", ("off" if enabled else "on", chatid))
    return not enabled


def alert_status(chatid, user_id):
    with connect() as conn:
        row = conn.execute("SELECT status FROM alert_state WHERE chatid=? AND user_id=?", (chatid, user_id)).fetchone()
        return row[0] if row else None


def record_status(chatid, user_id, status):
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO alert_state VALUES (?, ?, ?)", (chatid, user_id, status))


def update_login_password(chatid, admin_name, password):
    """Keep password-based bot authentication usable after changing its own login."""
    info = config(chatid)
    if not info["api_key"] and info["username"] == admin_name:
        with connect() as conn:
            conn.execute("UPDATE users SET password=? WHERE chatid=?", (password, chatid))
        _client.cache_clear()
