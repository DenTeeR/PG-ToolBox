"""PasarGuard v5.6.0 API. Contract: PasarGuard/panel tag v5.6.0.

All panel HTTP traffic goes through this client; Telegram never receives raw
validation bodies (which can contain passwords and proxy credentials).
"""

import os
import re
import threading
from decimal import Decimal, InvalidOperation, localcontext
from urllib.parse import quote, urlsplit

import requests

SUPPORTED_PANEL = "5.6.0"
MAX_DURATION = 2_147_483_647  # app/models/validators.py
MAX_TRAFFIC = 2**63 - 1  # app/db/models.py: signed BigInteger data_limit


class PanelError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def panel_url(value):
    value = value.strip().rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Enter the panel base URL, including https:// and any reverse proxy prefix.")
    return value


def username(value):
    if not re.fullmatch(r"[A-Za-z0-9_@.\-]{3,128}", value) or re.search(r"[-_@.]{2,}", value):
        raise ValueError("Username: 3–128 letters/digits or -, _, @, .; no consecutive symbols.")
    return value


def traffic_bytes(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise ValueError("Traffic must be a nonnegative number of GB; 0 means unlimited.")
        with localcontext() as context:
            context.prec = max(60, len(amount.as_tuple().digits) + 12)
            if amount > Decimal(MAX_TRAFFIC) / 1024**3:
                raise ValueError("Traffic exceeds the maximum supported by PasarGuard's database.")
            result = int(amount * 1024**3)
        if amount and result == 0:
            raise ValueError("A positive traffic limit must be at least one byte; use 0 only for unlimited traffic.")
        return result
    except (InvalidOperation, OverflowError):
        raise ValueError("Enter a valid traffic limit in GB.") from None


def canonical_traffic(value):
    with localcontext() as context:
        context.prec = 60
        text = format(Decimal(traffic_bytes(value)) / 1024**3, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def duration_seconds(days):
    if not str(days).isascii() or not str(days).isdigit():
        raise ValueError("Days must be a whole number; 0 means unlimited.")
    seconds = int(days) * 86400
    if seconds > MAX_DURATION:
        raise ValueError(f"The maximum duration is {MAX_DURATION // 86400} days.")
    return seconds


def create_payload(name, gb, days, group_ids):
    if not group_ids or any(type(item) is not int or item <= 0 for item in group_ids):
        raise ValueError("Select at least one enabled PasarGuard group.")
    duration = duration_seconds(days)
    return {
        "username": username(name),
        "group_ids": sorted(set(group_ids)),
        "data_limit": traffic_bytes(gb),
        "data_limit_reset_strategy": "no_reset",
        "status": "on_hold" if duration else "active",
        "expire": None,
        "on_hold_timeout": None,
        "on_hold_expire_duration": duration or None,
        "note": "by PG-ToolBox",
    }


class PasarGuardClient:
    def __init__(self, url, username="", password="", api_key="", session=None):
        self.url = panel_url(url)
        self.username, self.password, self.api_key = username, password, api_key
        self.session = session or requests.Session()
        self.token = None
        self.timeout = float(os.environ.get("PG_HTTP_TIMEOUT", "20"))
        tls = os.environ.get("PG_VERIFY_TLS", "true").lower()
        self.verify = False if tls == "false" else os.environ.get("PG_CA_BUNDLE", True)
        # A client is shared by Telegram's worker threads and background checks.
        self.lock = threading.RLock()

    def _send(self, method, path, **kwargs):
        try:
            return self.session.request(
                method, self.url + path, timeout=self.timeout, verify=self.verify, allow_redirects=False, **kwargs
            )
        except requests.RequestException:
            raise PanelError("Cannot reach PasarGuard. Check the panel URL, TLS certificate and network.") from None

    @staticmethod
    def _result(response):
        if not 200 <= response.status_code < 300:
            messages = {
                401: "Authentication failed. Check the API key or admin credentials.",
                403: "PasarGuard denied this operation. Check the admin role and API key permissions.",
                404: "The requested item or API endpoint was not found.",
                409: "The item already exists or conflicts with the current panel state.",
                422: "PasarGuard rejected the fields. Check names, password policy, groups and role.",
                429: "PasarGuard request limit reached. Try again later or check the API key quota.",
            }
            raise PanelError(
                messages.get(response.status_code, f"PasarGuard returned HTTP {response.status_code}."),
                response.status_code,
            )
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            raise PanelError("The panel did not return JSON. Check the panel base URL.") from None

    def _login(self):
        if not self.username or not self.password:
            raise PanelError("Configure a PasarGuard API key or admin username and password.", 401)
        data = self._result(
            self._send(
                "POST",
                "/api/admin/token",
                data={
                    "username": self.username,
                    "password": self.password,
                },
            )
        )
        if not isinstance(data, dict) or not data.get("access_token"):
            raise PanelError("PasarGuard returned an invalid authentication response.")
        self.token = data["access_token"]

    def request(self, method, path, **kwargs):
        if not path.startswith("/api/"):
            raise ValueError("Only PasarGuard API paths are allowed.")
        with self.lock:
            if self.api_key:
                headers = {"X-API-Key": self.api_key}
            else:
                if not self.token:
                    self._login()
                headers = {"Authorization": f"Bearer {self.token}"}
            response = self._send(method, path, headers=headers, **kwargs)
            if response.status_code == 401 and not self.api_key:
                self._login()
                response = self._send(method, path, headers={"Authorization": f"Bearer {self.token}"}, **kwargs)
            return self._result(response)

    def list_all(self, path, key, **params):
        items, offset = [], 0
        while True:
            data = self.request("GET", path, params={**params, "offset": offset, "limit": 100})
            if (
                not isinstance(data, dict)
                or not isinstance(data.get(key), list)
                or not isinstance(data.get("total"), int)
            ):
                raise PanelError(f"Unexpected PasarGuard response for {key}; expected a paginated object.")
            page = data[key]
            if not page and offset < data["total"]:
                raise PanelError("PasarGuard returned an incomplete page. Retry the operation.")
            items.extend(page)
            offset += len(page)
            if offset >= data["total"]:
                return items

    def current_admin(self):
        return self.request("GET", "/api/admin")

    def users(self, **params):
        return self.list_all("/api/users", "users", sort="username", **params)

    @staticmethod
    def user_path(identity):
        if isinstance(identity, int):
            return f"/api/user/by-id/{identity}"
        return "/api/user/by-username/" + quote(identity, safe="")

    def user(self, identity):
        return self.request("GET", self.user_path(identity))

    def find_user(self, text):
        if text.startswith(("https://", "http://")):
            # Resolve against panel-provided subscription URLs. Never send admin
            # credentials to a URL or host supplied in a Telegram message.
            token = urlsplit(text).path.rstrip("/").rsplit("/", 1)[-1]
            if token:
                for user in self.users(load_sub=True):
                    sub = urlsplit(user.get("subscription_url", "")).path.rstrip("/").rsplit("/", 1)[-1]
                    if sub and sub == token:
                        return self.user(user["id"])
            raise PanelError("No matching subscription was found.", 404)
        return self.user(text)

    def create_user(self, name, gb, days, group_ids):
        data = create_payload(name, gb, days, group_ids)
        available = {g["id"] for g in self.groups() if not g.get("is_disabled")}
        if not set(data["group_ids"]) <= available:
            raise ValueError("A selected group was removed, disabled or is inaccessible. Select groups again.")
        return self.request("POST", "/api/user", json=data)

    def delete_user(self, identity):
        return self.request("DELETE", self.user_path(identity))

    def last_subscription_update(self, identity):
        data = self.request("GET", self.user_path(identity) + "/sub_update", params={"limit": 1, "offset": 0})
        return next(iter(data["updates"]), None)

    def clients(self):
        # This endpoint aggregates user agents; no request per user is needed.
        return self.request("GET", "/api/users/sub_update/chart")["segments"]

    def groups(self):
        return self.list_all("/api/groups", "groups")

    def admins(self, **params):
        return self.list_all("/api/admins", "admins", **params)

    def admin(self, admin_id):
        admins = self.admins(ids=[admin_id])
        if not admins:
            raise PanelError("Admin not found.", 404)
        return admins[0]

    def roles(self):
        return self.list_all("/api/admin-roles", "roles")

    def check_role(self, role_id):
        if not any(r["id"] == role_id and not r["is_owner"] for r in self.roles()):
            raise ValueError("Select a non-owner PasarGuard role. The API cannot assign the owner role.")

    def create_admin(self, name, password, role_id):
        self.check_role(role_id)
        return self.request(
            "POST",
            "/api/admin",
            json={
                "username": name,
                "password": password,
                "role_id": role_id,
            },
        )

    def update_admin(self, admin_id, **fields):
        if "role_id" in fields:
            self.check_role(fields["role_id"])
        return self.request("PUT", f"/api/admin/by-id/{admin_id}", json=fields)

    def delete_admin(self, admin_id):
        return self.request("DELETE", f"/api/admin/by-id/{admin_id}")

    def nodes(self):
        return self.list_all("/api/nodes", "nodes")

    def node(self, node_id):
        return self.request("GET", f"/api/node/{node_id}")

    def update_node(self, node_id, **fields):
        return self.request("PUT", f"/api/node/{node_id}", json=fields)

    def reconnect_node(self, node_id):
        return self.request("POST", f"/api/node/{node_id}/reconnect")

    def announcement(self):
        return self.request("GET", "/api/settings")["subscription"].get("announce", "")

    def set_announcement(self, text):
        if len(text) > 128:
            raise ValueError("Announcements have a maximum of 128 characters.")
        subscription = self.request("GET", "/api/settings")["subscription"]
        # Subscription.rules is required. Preserve all the existing subscription
        # fields and leave unrelated settings sections out of the PUT.
        subscription["announce"] = text
        return self.request("PUT", "/api/settings", json={"subscription": subscription})
