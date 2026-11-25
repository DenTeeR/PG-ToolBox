import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from Function import db
from Function.api import PasarGuardClient
from setup import save_config

CONTRACT = json.loads((Path(__file__).parent / "contracts/pasarguard-v5.6.0.json").read_text())


def model_fields(name):
    model = CONTRACT["models"].get(name, {})
    fields = set(model.get("fields", []))
    required = set(model.get("required", []))
    for parent in model.get("bases", []):
        parent_fields, parent_required = model_fields(parent)
        fields |= parent_fields
        required |= parent_required
    # A field overridden with a default is no longer required.
    required -= set(model.get("fields", [])) - set(model.get("required", []))
    return fields, required


class Reply:
    def __init__(self, data=None, code=200):
        self.status_code = code
        self.data = data
        self.content = json.dumps(data).encode() if data is not None else b""

    def json(self):
        return self.data


USER = {
    "id": 42,
    "username": "alice",
    "status": "active",
    "used_traffic": 1024,
    "data_limit": 1024**3,
    "group_ids": [8],
    "expire": "2026-12-01T00:00:00Z",
    "online_at": "2026-11-25T10:00:00+03:30",
    "subscription_url": "https://sub.example/custom/token42",
}


class ContractSession:
    """Rejects method/path/field mismatches with the actual upstream source snapshot."""

    def __init__(self):
        self.calls = []
        self.users = [dict(USER)]
        self.groups = [{"id": 8, "name": "main", "is_disabled": False, "inbound_tags": ["vless"]}]
        self.roles = [{"id": 1, "name": "Owner", "is_owner": True}, {"id": 7, "name": "Operator", "is_owner": False}]
        self.subscription = {
            "announce": "old",
            "rules": [{"pattern": ".*", "target": "v2ray"}],
            "profile_title": "keep me",
        }

    def request(self, method, url, **kwargs):
        path = urlsplit(url).path.removeprefix("/prefix")
        assert any(
            route["method"] == method and re.fullmatch(re.sub(r"\{[^}]+\}", r"[^/]+", route["path"]), path)
            for route in CONTRACT["routes"]
        ), (method, path)
        self.calls.append((method, path, kwargs))
        payload = kwargs.get("json")
        model = None
        if method in {"POST", "PUT"} and path.startswith("/api/user"):
            model = "UserCreate" if method == "POST" else "UserModify"
        elif method in {"POST", "PUT"} and path.startswith("/api/admin") and path != "/api/admin/token":
            model = "AdminCreate" if method == "POST" else "AdminModify"
        elif method == "PUT" and path.startswith("/api/node/"):
            model = "NodeModify"
        elif method == "PUT" and path == "/api/settings":
            model = "SettingsSchema"
        if model:
            fields, required = model_fields(model)
            assert set(payload) <= fields, (model, set(payload) - fields)
            assert required <= set(payload), (model, required - set(payload))
        if path == "/api/admin/token":
            return Reply({"access_token": "token"})
        if path == "/api/admin":
            return Reply({"username": "boss"}, 201 if method == "POST" else 200)
        if method == "DELETE":
            return Reply(code=204)
        if method == "POST" and path == "/api/user":
            new_user = {**USER, **payload, "id": 50 + len(self.users)}
            self.users.append(new_user)
            return Reply(new_user, 201)
        if path.endswith("/sub_update"):
            return Reply({"updates": [{"created_at": "2026-11-25T06:30:00Z", "user_agent": "Happ"}], "count": 1})
        if path == "/api/users/sub_update/chart":
            return Reply({"total": 4, "segments": [{"name": "Happ", "count": 4, "percentage": 100}], "stats": []})
        if path.startswith("/api/user/"):
            return Reply(self.users[0])
        if path == "/api/node/3/reconnect":
            return Reply({"detail": "accepted"}, 202)
        if path == "/api/node/3":
            return Reply(
                {"id": 3, "name": "node", "address": "127.0.0.1", "status": "connected", "usage_coefficient": 1}
            )
        if path == "/api/settings":
            if method == "PUT":
                self.subscription = payload["subscription"]
            return Reply({"subscription": self.subscription, "telegram": {"token": "never put back"}})
        if path.startswith("/api/admin/by-id/"):
            return Reply({"username": "operator", "role": {"id": 7, "name": "Operator"}})
        collections = {
            "/api/users": ("users", self.users),
            "/api/groups": ("groups", self.groups),
            "/api/admin-roles": ("roles", self.roles),
            "/api/admins": ("admins", [{"id": 2, "username": "operator", "status": "active", "role": self.roles[1]}]),
            "/api/nodes": (
                "nodes",
                [{"id": 3, "name": "node", "address": "127.0.0.1", "status": "connected", "usage_coefficient": 1}],
            ),
        }
        if path in collections:
            key, values = collections[path]
            offset = kwargs.get("params", {}).get("offset", 0)
            # Deliberately cap the server page below the requested page size.
            return Reply({key: values[offset : offset + 1], "total": len(values)})
        raise AssertionError((method, path))


@pytest.fixture
def api():
    return PasarGuardClient("https://panel.example/prefix", api_key="pg_key_test", session=ContractSession())


@pytest.fixture
def configured(tmp_path, monkeypatch, api):
    monkeypatch.setenv("PG_DB", str(tmp_path / "test.db"))
    save_config(100, "123:test", "https://panel.example/prefix", api_key="pg_key_test")
    monkeypatch.setattr(db, "client_for", lambda chatid=None: api)
    return api
