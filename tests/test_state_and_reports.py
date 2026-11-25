import sqlite3
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from Function import db
from Function.api import PanelError
from Function.users import pdf_report, recent, summary, time_report, user_text, utc_datetime
from monitoring import check_nodes
from notices import pending_notices


def test_migration_preserves_old_templates_and_requires_new_panel_configuration(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    monkeypatch.setenv("PG_DB", str(path))
    with sqlite3.connect(path) as conn:
        conn.executescript("""
        CREATE TABLE users (chatid INTEGER PRIMARY KEY, role TEXT, name TEXT,
            username TEXT, password TEXT, domain TEXT, step TEXT);
        INSERT INTO users VALUES (100, 'boss', 'owner', 'oldadmin', 'oldpass', 'https://oldpanel.example', 'old step');
        CREATE TABLE templates (name TEXT PRIMARY KEY, data INTEGER, date INTEGER, proxies TEXT, inbounds TEXT);
        INSERT INTO templates VALUES ('old', 1, 30, '{}', '{}');
        CREATE TABLE messages (chatid INTEGER PRIMARY KEY, status TEXT);
        INSERT INTO messages VALUES (100, 'on');
        """)
    db.initialize()
    db.initialize()  # Idempotent; does not reset future conversation state.
    assert not db.authorized(100)
    assert db.templates() == []
    assert not db.alerts_enabled(100)
    with pytest.raises(ValueError, match="setup.py"):
        db.config()
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM templates").fetchone()[0] == 1
        assert conn.execute("SELECT password FROM users").fetchone()[0] == "oldpass"
    assert path.stat().st_mode & 0o777 == 0o600


def test_naive_aware_and_unlimited_dates():
    now = datetime(2026, 11, 25, 7, tzinfo=timezone.utc)
    assert utc_datetime("2026-11-25T10:30:00+03:30") == now
    assert utc_datetime("2026-11-25T07:00:00Z") == now
    assert utc_datetime("2026-11-25T07:00:00") == now
    assert recent("2026-11-25T06:59:01Z", 60, now)
    assert not recent(None, 60, now)
    assert not recent("2026-11-25T08:00:00Z", 60, now)
    text = user_text(
        {
            "username": "<alice>",
            "status": "on_hold",
            "data_limit": None,
            "on_hold_expire_duration": 86400,
            "group_ids": [8],
        }
    )
    assert "&lt;alice&gt;" in text and "unlimited" in text
    assert "first connection: 1 days" in text


def test_history_reports_use_subscription_update_endpoint_and_include_never_users():
    api = Mock()
    api.users.return_value = [{"id": 1, "username": "recent"}, {"id": 2, "username": "never"}]
    api.last_subscription_update.side_effect = [{"created_at": datetime.now(timezone.utc).isoformat()}, None]
    yes, no = time_report(api, "subscription", 3600)
    assert yes == ["recent"] and no == ["never"]
    assert api.last_subscription_update.call_count == 2
    pdf = pdf_report(yes)
    assert pdf.read(4) == b"%PDF"
    pdf.close()


def test_user_summary_counts_offline_and_never_connected():
    api = Mock()
    api.users.return_value = [
        {"status": "active", "online_at": datetime.now(timezone.utc).isoformat()},
        {"status": "on_hold", "online_at": None},
    ]
    assert "Online within 24h: 1" in summary(api)
    assert "Offline / never connected within 24h: 1" in summary(api)


def test_expiry_alert_baseline_transition_and_acknowledgement(configured):
    assert db.toggle_alerts(100)
    assert pending_notices(100) == []
    configured.session.users[0]["status"] = "expired"
    pending = pending_notices(100)
    assert len(pending) == 1
    assert pending_notices(100) == pending  # Unsent alerts remain pending.
    db.record_status(100, pending[0][0], pending[0][1])
    assert pending_notices(100) == []
    configured.session.users[0]["status"] = "active"
    assert pending_notices(100) == []
    configured.session.users[0]["status"] = "limited"
    assert pending_notices(100)[0][1] == "limited"
    assert all(c[0] == "GET" for c in configured.session.calls)  # No account mutation.
    assert not db.toggle_alerts(100)
    assert pending_notices(100) == []


def test_monitor_reconnect_posts_and_accepts_202(configured, monkeypatch):
    db.set_monitoring(100, status="on")
    monkeypatch.setattr(
        configured,
        "nodes",
        lambda: [{"id": 3, "name": "<node>", "address": "localhost", "status": "error", "message": "oops"}],
    )
    messages, interval = check_nodes(100)
    assert "&lt;node&gt;" in messages[0]
    assert "Reconnect requested" in messages[0]
    assert interval == 120
    assert configured.session.calls[-1][:2] == ("POST", "/api/node/3/reconnect")
    monkeypatch.setattr(configured, "reconnect_node", Mock(side_effect=PanelError("denied", 403)))
    messages, _ = check_nodes(100)
    assert "Reconnect failed" in messages[0]
    db.set_monitoring(100, status="off")
    assert check_nodes(100)[0] == []
