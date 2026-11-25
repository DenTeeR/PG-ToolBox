"""Runtime cases missed by the initial migration test suite."""

import asyncio
import os
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from pyrogram.errors import FloodWait, QueryIdInvalid

import holder
import setup
from Function import db
from Function.telegram import make_client
from tests.test_bot import click, query, send, telegram


async def test_rotating_bot_token_does_not_reuse_authorized_telegram_session(configured):
    old = make_client()
    await old.storage.open()
    await old.storage.user_id(123)
    await old.storage.close()
    with db.connect() as conn:
        conn.execute("UPDATE bot SET token='456:replacement'")
    new = make_client()
    try:
        await new.storage.open()
        assert await new.storage.user_id() is None
        assert new.storage.database.parent == db.db_path().parent
        assert new.storage.database != old.storage.database
    finally:
        await new.storage.close()
        # Close and remove test sessions even against the original fixed name.
        for path in {old.storage.database, new.storage.database}:
            path.unlink(missing_ok=True)


async def test_flood_wait_resumes_delivery_without_recreating_panel_users(configured, monkeypatch):
    app = telegram()
    app.send_photo.side_effect = [FloodWait(12), None, None]
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)
    await holder.confirm(app, 100, "create", "Confirm?", name="batch", gb="1", days=1, groups=[8], count=2)
    context = db.state(100)
    await click(app, f"confirm|{context['nonce']}")
    created = [c[2]["json"]["username"] for c in configured.session.calls if c[:2] == ("POST", "/api/user")]
    assert created == ["batch1", "batch2"]
    assert app.send_photo.await_count == 3
    sleep.assert_awaited_once_with(12)
    assert any("Created 2/2" in call.args[1] for call in app.send_message.call_args_list)


async def test_expired_callback_answer_does_not_drop_a_valid_operation(configured):
    app = telegram()
    item = query("node|3")
    item.answer.side_effect = QueryIdInvalid()
    await holder.handle_callback(app, item)
    assert any(c[:2] == ("GET", "/api/node/3") for c in configured.session.calls)
    assert "Usage coefficient" in app.send_message.call_args.args[1]


async def test_oversized_qr_is_a_recoverable_input_error(configured):
    app = telegram()
    await send(app, "🖼 QR Code")
    await send(app, "🧪" * 2000)
    assert "shorter" in app.send_message.call_args.args[1].lower()
    assert db.state(100)["mode"] == "qr"
    await send(app, "https://example.com")
    app.send_photo.assert_awaited_once()


def test_configuration_backup_is_private_before_sqlite_opens_it(configured, monkeypatch):
    monkeypatch.setattr("builtins.input", Mock(side_effect=["100", "https://panel.example"]))
    monkeypatch.setattr(setup.getpass, "getpass", Mock(side_effect=["456:newbot", "pg_key_new"]))
    monkeypatch.setattr(setup, "PasarGuardClient", Mock(return_value=Mock(current_admin=lambda: {"username": "boss"})))
    original = sqlite3.connect
    modes = []

    def inspect_connect(path, *args, **kwargs):
        connection = original(path, *args, **kwargs)
        if ".backup-" in str(path):
            modes.append(Path(path).stat().st_mode & 0o777)
        return connection

    monkeypatch.setattr(sqlite3, "connect", inspect_connect)
    previous_umask = os.umask(0o022)
    try:
        setup.main()
    finally:
        os.umask(previous_umask)
    assert modes == [0o600]
    backup = next(db.db_path().parent.glob("*.backup-*.db"))
    with original(backup) as connection:
        assert connection.execute("SELECT token FROM bot").fetchone()[0] == "123:test"
    assert db.bot_token() == "456:newbot"


def test_unsupported_python_does_not_stop_an_existing_service(tmp_path):
    # Exercise the real script with harmless command shims. Only bypass its root
    # guard; version rejection guarantees the service unit is never written.
    source = (Path(__file__).parents[1] / "holderbot.sh").read_text()
    script = tmp_path / "install.sh"
    script.write_text(source.replace("if [[ ${EUID} -ne 0 ]]; then", "if false; then", 1))
    commands = tmp_path / "bin"
    commands.mkdir()
    log = tmp_path / "calls"
    for name in ("apt-get", "systemctl", "git"):
        stub = commands / name
        stub.write_text('#!/bin/bash\nprintf "%s\\n" "' + name + ' $*" >> "$TEST_INSTALL_LOG"\n')
        stub.chmod(0o755)
    python = commands / "unsupported-python"
    python.write_text('#!/bin/bash\necho "Unsupported Python" >&2\nexit 42\n')
    python.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{commands}:{os.environ['PATH']}",
        "PG_INSTALL_DIR": str(tmp_path / "app"),
        "PG_PYTHON": str(python),
        "TEST_INSTALL_LOG": str(log),
    }
    result = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
    assert result.returncode == 42
    assert "systemctl stop" not in log.read_text()
    assert "git clone" not in log.read_text()


async def test_delivery_retry_rewinds_photo_stream_and_preserves_session_identity(configured, monkeypatch):
    from io import BytesIO
    from Function.telegram import telegram_call

    before = make_client()
    after = make_client()
    assert before.storage.database == after.storage.database
    content = BytesIO(b"PNG-image-content")
    attempts = []

    async def upload(photo):
        attempts.append(photo.read())
        if len(attempts) == 1:
            raise FloodWait(1)
        return "sent"

    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    assert await telegram_call(upload, content) == "sent"
    assert attempts == [b"PNG-image-content", b"PNG-image-content"]


async def test_expired_acknowledgement_cannot_replay_consumed_confirmation(configured):
    app = telegram()
    await holder.confirm(app, 100, "delete_user", "Confirm?", id=42)
    state = db.state(100)
    first = query(f"confirm|{state['nonce']}")
    first.answer.side_effect = QueryIdInvalid()
    await holder.handle_callback(app, first)
    second = query(first.data)
    second.answer.side_effect = QueryIdInvalid()
    await holder.handle_callback(app, second)
    assert len([c for c in configured.session.calls if c[:2] == ("DELETE", "/api/user/by-id/42")]) == 1


def test_installer_rejects_paths_hidden_by_its_service_sandbox(tmp_path):
    source = (Path(__file__).parents[1] / "holderbot.sh").read_text()
    script = tmp_path / "install.sh"
    script.write_text(source.replace("if [[ ${EUID} -ne 0 ]]; then", "if false; then", 1))
    # All cases must fail before any privileged command. A rejecting apt shim
    # prevents an installer regression from installing anything on the host.
    commands = tmp_path / "bin"
    commands.mkdir()
    marker = tmp_path / "apt-called"
    apt = commands / "apt-get"
    apt.write_text(f'#!/bin/bash\ntouch "{marker}"\nexit 99\n')
    apt.chmod(0o755)
    for target in ("/root/pg-toolbox", "/home/user/pg-toolbox", "/run/user/1000/pg-toolbox", "/opt/../root/pg-toolbox"):
        result = subprocess.run(
            ["bash", str(script)],
            capture_output=True,
            text=True,
            env={**os.environ, "PATH": f"{commands}:{os.environ['PATH']}", "PG_INSTALL_DIR": target},
        )
        assert result.returncode == 1
        assert "outside" in result.stdout
    assert not marker.exists()


def test_positive_fraction_below_one_byte_cannot_create_unlimited_traffic():
    from Function.api import create_payload
    import pytest

    with pytest.raises(ValueError, match="byte"):
        create_payload("alice", "0.0000000001", 1, [8])


def test_traffic_limits_cannot_overflow_pasarguards_bigint_column():
    from Function.api import traffic_bytes
    import pytest

    for value in ("1e1000000", "8589934592"):
        with pytest.raises(ValueError, match="maximum"):
            traffic_bytes(value)


async def test_long_numeric_input_is_normalized_before_saving_conversation(configured):
    app = telegram()
    await click(app, "manual")
    await send(app, "alice")
    await send(app, "0" * 3900 + "1")
    assert db.state(100)["gb"] == "1"


async def test_large_group_lists_fit_telegram_without_changing_selection(configured):
    from Function.users import user_text

    app = telegram()
    groups = list(range(1, 1001))
    await holder.confirm_creation(app, 100, {"name": "alice", "count": 1, "gb": "1", "days": 1, "groups": groups})
    assert len(app.send_message.call_args.args[1]) < 4096
    assert db.state(100)["groups"] == groups
    text = user_text({"username": "alice", "status": "active", "group_ids": groups})
    assert len(text) < 4096


def test_minimum_and_maximum_traffic_round_trip_without_changing_quota():
    from decimal import Decimal, localcontext
    from Function.api import MAX_TRAFFIC, canonical_traffic, traffic_bytes

    with localcontext() as context:
        context.prec = 60
        for quota in (1, MAX_TRAFFIC):
            gb = str(Decimal(quota) / 1024**3)
            assert traffic_bytes(gb) == quota
            assert traffic_bytes(canonical_traffic(gb)) == quota


def test_failed_backup_preserves_configuration_and_removes_partial_copy(configured, monkeypatch):
    import pytest

    monkeypatch.setattr("builtins.input", Mock(side_effect=["100", "https://panel.example"]))
    monkeypatch.setattr(setup.getpass, "getpass", Mock(side_effect=["456:newbot", "pg_key_new"]))
    monkeypatch.setattr(setup, "PasarGuardClient", Mock(return_value=Mock(current_admin=lambda: {"username": "boss"})))
    original = sqlite3.connect
    broken_source = Mock(backup=Mock(side_effect=sqlite3.OperationalError("disk full")))

    def connect(path, *args, **kwargs):
        if Path(path) == db.db_path():
            return broken_source
        return original(path, *args, **kwargs)

    with monkeypatch.context() as temporary:
        temporary.setattr(sqlite3, "connect", connect)
        with pytest.raises(sqlite3.OperationalError, match="disk full"):
            setup.main()
    broken_source.close.assert_called_once()
    assert not list(db.db_path().parent.glob("*.backup-*.db"))
    assert db.bot_token() == "123:test"


async def test_other_delivery_errors_are_not_blindly_retried():
    import pytest
    from Function.telegram import telegram_call

    method = AsyncMock(side_effect=ConnectionError("uncertain delivery"))
    with pytest.raises(ConnectionError):
        await telegram_call(method, "message")
    method.assert_awaited_once()


def test_long_numeric_qr_that_fits_is_still_accepted():
    from Function.qr import DEF_CREATE_QRCODE

    image = DEF_CREATE_QRCODE("1" * 4000)
    try:
        assert image.read(8) == b"\x89PNG\r\n\x1a\n"
    finally:
        image.close()
