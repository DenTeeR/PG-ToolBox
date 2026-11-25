"""Notify the bot owner about transitions, without modifying panel accounts."""

from html import escape

from Function import db


def pending_notices(chatid):
    if not db.alerts_enabled(chatid):
        return []
    pending = []
    for user in db.client_for(chatid).users():
        if user["status"] in {"expired", "limited"} and db.alert_status(chatid, user["id"]) != user["status"]:
            pending.append(
                (user["id"], user["status"], f"<b>{escape(user['username'])}</b> is now {escape(user['status'])}.")
            )
        else:
            db.record_status(chatid, user["id"], user["status"])
    return pending
