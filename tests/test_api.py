import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import Mock
from urllib.parse import parse_qs

import pytest
import requests

from Function.api import MAX_DURATION, PanelError, PasarGuardClient, create_payload, panel_url
from tests.conftest import Reply


def test_all_client_operations_match_official_routes_and_models(api):
    assert api.current_admin()["username"] == "boss"
    assert api.users()[0]["username"] == "alice"
    assert api.user("user@domain.com")["id"] == 42
    assert api.user(42)["id"] == 42
    assert api.find_user("https://other.example/custom/token42")["id"] == 42
    assert api.last_subscription_update(42)["user_agent"] == "Happ"
    assert api.clients()[0]["name"] == "Happ"
    assert len(api.roles()) == 2  # The fake server caps pages at one item.
    assert api.admin(2)["username"] == "operator"
    api.create_admin("newadmin", "LongPA12ssword!", 7)
    api.update_admin(2, role_id=7)
    api.update_admin(2, password="LongPA12ssword!")
    api.delete_admin(2)  # HTTP 204 with an empty body is success.
    assert api.nodes()[0]["id"] == 3
    api.node(3)
    api.update_node(3, status="disabled")
    api.update_node(3, status="connected")
    api.update_node(3, usage_coefficient=0.5)
    api.reconnect_node(3)  # HTTP 202 is accepted, not proof of connection.
    created = api.create_user("bob", "2.5", 30, [8])
    assert created["status"] == "on_hold"
    assert api.announcement() == "old"
    api.set_announcement("new")
    assert api.announcement() == "new"
    api.delete_user(42)
    calls = api.session.calls
    create = next(c for c in calls if c[:2] == ("POST", "/api/user"))
    assert create[2]["json"]["data_limit"] == int(2.5 * 1024**3)
    assert create[2]["json"]["on_hold_timeout"] is None
    assert "proxy_settings" not in create[2]["json"]  # Upstream generates protocol credentials.
    settings = next(c[2]["json"] for c in calls if c[:2] == ("PUT", "/api/settings"))
    assert set(settings) == {"subscription"}
    assert settings["subscription"]["profile_title"] == "keep me"
    assert settings["subscription"]["rules"]
    assert all(c[2]["verify"] is True and not c[2]["allow_redirects"] for c in calls)
    assert all(c[2]["headers"] == {"X-API-Key": "pg_key_test"} for c in calls)


def test_password_auth_refreshes_only_once_and_does_not_change_method():
    session = Mock()
    session.request.side_effect = [
        Reply({"access_token": "old"}),
        Reply(code=401),
        Reply({"access_token": "new"}),
        Reply(code=204),
    ]
    api = PasarGuardClient("https://panel.example", "admin", "pass&word=42", session=session)
    api.delete_user(42)
    assert [c.args[0] for c in session.request.call_args_list] == ["POST", "DELETE", "POST", "DELETE"]
    assert session.request.call_args_list[0].kwargs["data"]["password"] == "pass&word=42"
    assert session.request.call_args_list[-1].kwargs["headers"]["Authorization"] == "Bearer new"


@pytest.mark.parametrize("status", [401, 403, 409, 422, 429, 500, 302])
def test_errors_do_not_leak_payloads_or_credentials(status):
    session = Mock()
    session.request.return_value = Reply({"detail": [{"input": "secret password"}]}, status)
    api = PasarGuardClient("https://panel.example", api_key="key", session=session)
    with pytest.raises(PanelError) as error:
        api.current_admin()
    assert error.value.status == status
    assert "secret" not in str(error.value)
    assert session.request.call_count == 1


def test_network_failure_is_sanitized():
    session = Mock()
    session.request.side_effect = requests.Timeout("secret url")
    api = PasarGuardClient("https://panel.example", api_key="key", session=session)
    with pytest.raises(PanelError, match="Cannot reach"):
        api.current_admin()


def test_no_empty_or_disabled_group_or_owner_role_assignments(api):
    with pytest.raises(ValueError):
        api.create_user("bob", 1, 1, [])
    api.session.groups[0]["is_disabled"] = True
    with pytest.raises(ValueError):
        api.create_user("bob", 1, 1, [8])
    with pytest.raises(ValueError):
        api.create_admin("operator", "LongPA12ssword!", 1)
    assert not any(c[0] == "POST" for c in api.session.calls)


def test_unlimited_and_duration_boundaries():
    payload = create_payload("alice", 0, 0, [8])
    assert payload["status"] == "active" and payload["on_hold_expire_duration"] is None
    assert payload["data_limit"] == 0
    assert create_payload("alice", 1, MAX_DURATION // 86400, [8])["status"] == "on_hold"
    with pytest.raises(ValueError):
        create_payload("alice", 1, MAX_DURATION // 86400 + 1, [8])
    for value in ("nan", "Infinity", "-1"):
        with pytest.raises(ValueError):
            create_payload("alice", value, 1, [8])


def test_subscription_lookup_never_requests_user_supplied_host(api):
    with pytest.raises(PanelError):
        api.find_user("https://evil.example/private/no-such-token")
    assert all(path.startswith("/api/") for _, path, _ in api.session.calls)


def test_incomplete_pagination_is_an_error():
    session = Mock()
    session.request.return_value = Reply({"users": [], "total": 10})
    with pytest.raises(PanelError, match="incomplete"):
        PasarGuardClient("https://panel.example", api_key="key", session=session).users()


def test_real_http_form_encoding_and_empty_delete_response():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
            received.append((self.path, parse_qs(body)))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"access_token":"test-token"}')

        def do_DELETE(self):
            received.append((self.path, self.headers.get("Authorization")))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        api = PasarGuardClient(f"http://127.0.0.1:{server.server_port}", "admin", "pass&word=42")
        assert api.delete_user(42) is None
        assert received == [
            ("/api/admin/token", {"username": ["admin"], "password": ["pass&word=42"]}),
            ("/api/user/by-id/42", "Bearer test-token"),
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize(
    "url", ["example.com", "https://user:pass@panel.example", "https://panel.example?token=x", "file:///tmp/x"]
)
def test_panel_url_rejects_ambiguous_or_credential_urls(url):
    with pytest.raises(ValueError):
        panel_url(url)
