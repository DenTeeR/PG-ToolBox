"""Interactive local configuration, separate from Python packaging."""

import getpass
import os
import sqlite3
from datetime import datetime, timezone
from contextlib import closing

from Function import db
from Function.api import PasarGuardClient, PanelError, panel_url


def save_config(chatid, token, url, admin_name="", password="", api_key=""):
    db.initialize()
    with db.connect() as conn:
        conn.execute("UPDATE users SET role='disabled', step='None'")
        conn.execute(
            """INSERT OR REPLACE INTO users
            (chatid, role, name, username, password, domain, step, api_key, backend)
            VALUES (?, 'boss', 'Owner', ?, ?, ?, 'None', ?, 'pasarguard')""",
            (chatid, admin_name, password, url, api_key),
        )
        conn.execute("DELETE FROM bot")
        conn.execute("INSERT INTO bot VALUES (?, ?)", (chatid, token))
        conn.execute("INSERT OR REPLACE INTO monitoring VALUES (?, 'off', 30, 120)", (chatid,))
        conn.execute("INSERT OR REPLACE INTO messages VALUES (?, 'off')", (chatid,))
        conn.execute("DELETE FROM alert_state WHERE chatid=?", (chatid,))
    db._client.cache_clear()


def main():
    print("PG-ToolBox · PasarGuard 5.6.0 configuration")
    raw_chatid = input("Your private Telegram user/chat ID: ").strip()
    if not raw_chatid.isascii() or not raw_chatid.isdigit() or int(raw_chatid) <= 0:
        raise ValueError("Use your positive private Telegram user ID.")
    token = getpass.getpass("Telegram bot token: ").strip()
    if not token or ":" not in token:
        raise ValueError("Invalid Telegram bot token format.")
    url = panel_url(input("PasarGuard base URL (https://panel.example.com): "))
    api_key = getpass.getpass("PasarGuard API key (blank to use admin login): ").strip()
    admin_name = password = ""
    if not api_key:
        admin_name = input("PasarGuard admin username: ").strip()
        password = getpass.getpass("PasarGuard admin password: ")
    api = PasarGuardClient(url, admin_name, password, api_key)
    admin = api.current_admin()
    print(f"PasarGuard authentication succeeded for {admin['username']}.")
    path = db.db_path()
    if path.exists():
        backup = path.with_name(f"{path.stem}.backup-{datetime.now(timezone.utc):%Y%m%d%H%M%S%f}.db")
        descriptor = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        try:
            with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(backup)) as destination:
                source.backup(destination)
        except BaseException:
            backup.unlink(missing_ok=True)
            raise
        print(f"Previous database backed up to {backup.name}.")
    save_config(int(raw_chatid), token, url, admin_name, password, api_key)
    print("Saved. Start the service, open your Telegram bot and send /start.")
    print("Monitoring and expiry alerts start disabled; enable them in the bot when ready.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, PanelError) as exc:
        raise SystemExit(str(exc)) from None
