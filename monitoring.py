"""Node checks and reconnect requests using PasarGuard's node API."""

from html import escape

from Function import db
from Function.api import PanelError


def check_nodes(chatid):
    settings = db.monitoring(chatid)
    if settings["status"] != "on":
        return [], 30
    api = db.client_for(chatid)
    messages = []
    for node in api.nodes():
        if node["status"] not in {"error", "connecting"}:
            continue
        text = (
            f"<b>Node alert: {escape(node['name'])}</b>\n"
            f"State: {escape(node['status'])}\n"
            f"Address: {escape(node['address'])}\n"
            f"{escape(node.get('message') or '')}"
        )
        if node["status"] == "error":
            try:
                api.reconnect_node(node["id"])
                text += "\nReconnect requested; the next check will verify its state."
            except PanelError as exc:
                text += "\nReconnect failed: " + escape(str(exc))
        messages.append(text)
    return messages, max(10, settings["check_error"] if messages else settings["check_normal"])
