import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import holder
from Function import db


def telegram():
    return SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock(), send_document=AsyncMock())


def message(text, chatid=100, actor=None):
    return SimpleNamespace(
        text=text,
        caption=None,
        chat=SimpleNamespace(id=chatid),
        from_user=SimpleNamespace(id=chatid if actor is None else actor),
        delete=AsyncMock(),
    )


def query(data, chatid=100):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=chatid),
        message=SimpleNamespace(chat=SimpleNamespace(id=chatid)),
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )


async def send(app, text):
    item = message(text)
    await holder.handle_message(app, item)
    return item


async def click(app, data):
    item = query(data)
    await holder.handle_callback(app, item)
    return item


async def choose_group(app):
    context = db.state(100)
    await click(app, f"g|{context['nonce']}|8")
    await click(app, f"gs|{context['nonce']}")


async def test_manual_create_and_consumed_confirmation(configured):
    app = telegram()
    await send(app, "/start")
    await click(app, "manual")
    for text in ("bob", "2.5", "30", "1"):
        await send(app, text)
    assert db.state(100)["mode"] == "groups"
    assert db.state(100)["groups"] == []
    await choose_group(app)
    context = db.state(100)
    assert context["mode"] == "confirm"
    await click(app, f"confirm|{context['nonce']}")
    assert db.state(100) == {}
    creates = [call for call in configured.session.calls if call[:2] == ("POST", "/api/user")]
    assert len(creates) == 1
    assert creates[0][2]["json"]["group_ids"] == [8]
    app.send_photo.assert_awaited_once()
    await click(app, f"confirm|{context['nonce']}")
    assert len([c for c in configured.session.calls if c[:2] == ("POST", "/api/user")]) == 1


async def test_templates_and_bulk_create(configured):
    app = telegram()
    await click(app, "templatenew")
    for text in ("Monthly", "10", "30"):
        await send(app, text)
    await choose_group(app)
    assert db.templates()[0]["group_ids"] == [8]
    await send(app, "🚀 Create User")
    await click(app, "t|Monthly")
    await send(app, "bulk")
    await send(app, "2")
    context = db.state(100)
    await click(app, f"confirm|{context['nonce']}")
    names = [c[2]["json"]["username"] for c in configured.session.calls if c[:2] == ("POST", "/api/user")]
    assert names == ["bulk1", "bulk2"]
    await send(app, "🗃 Templates")
    await click(app, "t|Monthly")
    context = db.state(100)
    assert db.templates()  # The initial click alone must not delete.
    await click(app, f"confirm|{context['nonce']}")
    assert db.templates() == []


async def test_user_stats_qr_and_204_delete(configured):
    app = telegram()
    await send(app, "alice")
    assert "Last subscription update:" in app.send_message.call_args.args[1]
    await click(app, "qr|42")
    app.send_photo.assert_awaited_once()
    await click(app, "udel|42")
    assert not any(c[0] == "DELETE" for c in configured.session.calls)
    state = db.state(100)
    await click(app, f"confirm|{state['nonce']}")
    assert any(c[:2] == ("DELETE", "/api/user/by-id/42") for c in configured.session.calls)
    assert "deleted" in app.send_message.call_args.args[1]


async def test_admin_creation_uses_non_owner_role_and_preserves_password(configured):
    app = telegram()
    await click(app, "adminnew")
    await send(app, "operator")
    state = db.state(100)
    assert state["role_ids"] == [7]
    await click(app, f"role|{state['nonce']}|7")
    password = "  AAaa12!!Bbbb  "
    password_message = await send(app, password)
    created = next(c for c in configured.session.calls if c[:2] == ("POST", "/api/admin"))
    assert created[2]["json"] == {"username": "operator", "password": password, "role_id": 7}
    password_message.delete.assert_awaited_once()
    assert password not in json.dumps(db.state(100))
    assert "Admin saved" in app.send_message.call_args.args[1]


async def test_admin_role_change_and_delete(configured):
    app = telegram()
    await click(app, "admin|2")
    await click(app, "arole|2")
    state = db.state(100)
    await click(app, f"role|{state['nonce']}|7")
    assert any(
        c[:2] == ("PUT", "/api/admin/by-id/2") and c[2]["json"] == {"role_id": 7} for c in configured.session.calls
    )
    await click(app, "adel|2")
    state = db.state(100)
    await click(app, f"confirm|{state['nonce']}")
    assert any(c[:2] == ("DELETE", "/api/admin/by-id/2") for c in configured.session.calls)


async def test_node_actions_reports_announcements_and_monitoring(configured):
    app = telegram()
    for text in (
        "🎗 Nodes",
        "👨🏻‍💻 Admins",
        "👤 Users",
        "✅ Active",
        "💻 Clients",
        "👀 Online time list",
        "60 day",
        "🎖 Notice",
        "🎛 Monitoring",
    ):
        await send(app, text)
    assert app.send_document.await_count >= 2
    await click(app, "node|3")
    for action in ("ndisable|3", "nenable|3", "nreconnect|3", "nusage|3"):
        await click(app, action)
    await send(app, "0.5")
    await click(app, "announceedit")
    await send(app, "Hello <world>")
    assert configured.session.subscription["announce"] == "Hello <world>"
    await click(app, "mtoggle")
    assert db.monitoring(100)["status"] == "on"
    await click(app, "mtimer|check_normal")
    await send(app, "45")
    assert db.monitoring(100)["check_normal"] == 45
    assert not any(call.args[1].startswith("The operation failed") for call in app.send_message.call_args_list)


async def test_unauthorized_messages_and_callbacks_cannot_call_panel(configured):
    app = telegram()
    await holder.handle_message(app, message("alice", chatid=999))
    await holder.handle_message(app, message("alice", actor=999))
    await holder.handle_callback(app, query("udel|42", chatid=999))
    assert configured.session.calls == []
    app.send_message.assert_not_awaited()


async def test_stale_group_callback_after_cancel_cannot_change_state(configured):
    app = telegram()
    await click(app, "manual")
    for text in ("alice", "1", "1", "1"):
        await send(app, text)
    state = db.state(100)
    await send(app, "/cancel")
    await click(app, f"g|{state['nonce']}|8")
    assert db.state(100) == {}
    assert "expired" in app.send_message.call_args.args[1]


@pytest.mark.parametrize("value", ["0", "501", "-2", "NaN"])
async def test_invalid_batch_sizes_do_not_call_create(configured, value):
    app = telegram()
    db.set_state(100, {"mode": "count", "name": "batch", "purpose": "create", "days": 1, "gb": "1"})
    await send(app, value)
    assert not any(c[0] == "POST" for c in configured.session.calls)
    assert db.state(100)["mode"] == "count"


async def test_partial_bulk_failure_does_not_retry_created_users(configured, monkeypatch):
    app = telegram()
    original = configured.create_user
    calls = []

    def create(name, *args):
        calls.append(name)
        if len(calls) == 2:
            raise holder.PanelError("Request failed", 500)
        return original(name, *args)

    monkeypatch.setattr(configured, "create_user", create)
    await holder.confirm(app, 100, "create", "Confirm?", name="batch", gb="1", days=1, groups=[8], count=3)
    state = db.state(100)
    await click(app, f"confirm|{state['nonce']}")
    assert calls == ["batch1", "batch2"]
    assert db.state(100) == {}
    assert any("Created 1/3" in c.args[1] for c in app.send_message.call_args_list)


async def test_group_selection_preserves_second_page(configured):
    app = telegram()
    configured.session.groups = [{"id": i, "name": f"Group{i}", "is_disabled": False} for i in range(1, 13)]
    await click(app, "manual")
    for text in ("alice", "1", "1", "1"):
        await send(app, text)
    state = db.state(100)
    await click(app, f"gp|{state['nonce']}|1")
    selected = await click(app, f"g|{state['nonce']}|12")
    assert db.state(100)["groups"] == [12]
    markup = selected.edit_message_reply_markup.call_args.args[0]
    assert markup.inline_keyboard[1][0].text.startswith("✅")


async def test_changing_bot_admin_password_updates_local_login(configured):
    app = telegram()
    with db.connect() as conn:
        conn.execute("UPDATE users SET username='operator', password='old', api_key='' WHERE chatid=100")
    await click(app, "apass|2")
    await send(app, "AAaa12!!Updated")
    assert db.config(100)["password"] == "AAaa12!!Updated"
