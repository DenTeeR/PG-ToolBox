"""Presentation and reports for PasarGuard's datetime and subscription models."""

from collections import Counter
from datetime import datetime, timezone
from difflib import SequenceMatcher
from html import escape
from io import BytesIO

from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle


def utc_datetime(value):
    if value is None or value == 0:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def recent(value, seconds, now=None):
    stamp = utc_datetime(value)
    return stamp is not None and 0 <= ((now or datetime.now(timezone.utc)) - stamp).total_seconds() <= seconds


def display_date(value):
    stamp = utc_datetime(value)
    return stamp.strftime("%Y-%m-%d %H:%M UTC") if stamp else "—"


def group_summary(group_ids):
    groups = group_ids or []
    text = ", ".join(map(str, groups[:20])) or "—"
    if len(groups) > 20:
        text += f" … ({len(groups)} groups total)"
    return text


def user_text(user, update=None):
    limit = user.get("data_limit")
    limit_text = f"{limit / 1024**3:.3f} GB" if limit else "unlimited"
    lines = [
        f"<b>{escape(user['username'])}</b> · {escape(user['status'])}",
        f"Traffic: {(user.get('used_traffic') or 0) / 1024**3:.3f} GB / {limit_text}",
        "Groups: " + escape(group_summary(user.get("group_ids"))),
    ]
    if user["status"] == "on_hold":
        lines.append(f"Validity after first connection: {(user.get('on_hold_expire_duration') or 0) / 86400:g} days")
    else:
        lines.append("Expires: " + (display_date(user["expire"]) if user.get("expire") else "unlimited"))
    lines.append("Last online: " + display_date(user.get("online_at")))
    lines.append("Last subscription update: " + display_date(update.get("created_at") if update else None))
    lines.append("App: " + escape(((update.get("user_agent") if update else None) or "—")[:300]))
    return "\n".join(lines)


def summary(api):
    users = api.users()
    statuses = Counter(user["status"] for user in users)
    lines = [f"<b>Users: {len(users)}</b>"]
    lines += [f"{status}: {statuses[status]}" for status in ("active", "on_hold", "disabled", "expired", "limited")]
    online = sum(recent(user.get("online_at"), 86400) for user in users)
    lines.append(f"Online within 24h: {online}")
    lines.append(f"Offline / never connected within 24h: {len(users) - online}")
    return "\n".join(lines)


def find_similar(api, text):
    names = [user["username"] for user in api.users()]
    names.sort(
        key=lambda name: (text.lower() in name.lower(), SequenceMatcher(None, text.lower(), name.lower()).ratio()),
        reverse=True,
    )
    return [
        name
        for name in names
        if text.lower() in name.lower() or SequenceMatcher(None, text.lower(), name.lower()).ratio() > 0.55
    ][:40]


def time_report(api, category, seconds):
    yes, no = [], []
    for user in api.users():
        if category == "subscription":
            update = api.last_subscription_update(user["id"])
            value = update["created_at"] if update else None
        else:
            value = user.get("online_at")
        (yes if recent(value, seconds) else no).append(user["username"])
    return sorted(yes), sorted(no)


def pdf_report(names):
    output = BytesIO()
    output.name = "pg-toolbox-users.pdf"
    rows = [[str(index), name] for index, name in enumerate(names, 1)]
    table = Table([["#", "Username"], *rows], colWidths=[45, 450], repeatRows=1)
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.3, "grey"), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    SimpleDocTemplate(output, pagesize=A4).build([table])
    output.seek(0)
    return output
