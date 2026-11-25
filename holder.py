"""PG-ToolBox: Telegram administration for PasarGuard v5.6.0."""

import asyncio
import logging
import re
import secrets
import sqlite3
from html import escape

from pyrogram import enums, filters, idle
from pyrogram.errors import MessageNotModified, RPCError

from Function import db
from Function.api import PanelError, SUPPORTED_PANEL, create_payload, duration_seconds, traffic_bytes, username
from Function.keyboards import CANCEL, HOME, TIMES, USERS, group_keyboard, inline, paged
from Function.qr import DEF_CREATE_QRCODE
from Function.telegram import make_client
from Function.users import find_similar, pdf_report, summary, time_report, user_text
from monitoring import check_nodes
from notices import pending_notices

log = logging.getLogger(__name__)
STATUS_BUTTONS = {
    "✅ Active": "active",
    "❌ Disabled": "disabled",
    "🕰 Expired": "expired",
    "🪫 Limited": "limited",
    "🔌 On Hold": "on_hold",
}


async def panel(chatid, method, *args, **kwargs):
    return await asyncio.to_thread(lambda: getattr(db.client_for(chatid), method)(*args, **kwargs))


async def say(app, chatid, text, keyboard=None):
    return await app.send_message(
        chatid, text, reply_markup=keyboard, parse_mode=enums.ParseMode.HTML, disable_web_page_preview=True
    )


async def send_pdf(app, chatid, names, title):
    if not names:
        await say(app, chatid, escape(title) + ": no users.")
        return
    document = await asyncio.to_thread(pdf_report, names)
    try:
        await app.send_document(chatid, document, caption=f"{title}: {len(names)} users")
    finally:
        document.close()


async def resource_list(app, chatid, kind, page=0):
    if kind == "node":
        items = await panel(chatid, "nodes")
        keyboard = paged(items, kind, lambda x: f"{x['name']} · {x['status']}", page)
        text = "Select a node."
    elif kind == "admin":
        items = await panel(chatid, "admins")
        keyboard = paged(
            items,
            kind,
            lambda x: f"{x['username']} · {(x.get('role') or {}).get('name', '—')}",
            page,
            extra=[("Add admin", "adminnew")],
        )
        text = "Select an admin. Permissions are controlled by PasarGuard roles."
    elif kind == "t":
        current = db.state(chatid)
        if current.get("mode") != "template_list":
            raise ValueError("Open Templates or Create User again.")
        items = [{**item, "id": item["name"]} for item in db.templates()]
        creating = current["purpose"] == "create"
        extra = [("Manual", "manual")] if creating else [("New template", "templatenew")]
        keyboard = paged(items, kind, lambda x: f"{x['name']} · {x['data']} GB · {x['days']} days", page, extra)
        text = "Select a template or create manually." if creating else "Select a template to delete, or add one."
    else:
        raise ValueError("Unknown list.")
    await say(app, chatid, text, keyboard)


async def select_groups(app, chatid, context):
    groups = [g for g in await panel(chatid, "groups") if not g.get("is_disabled")]
    if not groups:
        raise ValueError("No enabled, accessible PasarGuard groups. Create a group in the panel first.")
    context.update(mode="groups", nonce=secrets.token_hex(4), options=groups, groups=[], page=0)
    db.set_state(chatid, context)
    await say(app, chatid, "Select at least one PasarGuard group, then save.", group_keyboard(context))


async def select_role(app, chatid, context):
    roles = [r for r in await panel(chatid, "roles") if not r["is_owner"]]
    if not roles:
        raise ValueError("Create a non-owner admin role in PasarGuard first.")
    context.update(mode="role", nonce=secrets.token_hex(4), role_ids=[r["id"] for r in roles])
    db.set_state(chatid, context)
    rows = [[(r["name"], f"role|{context['nonce']}|{r['id']}")] for r in roles]
    await say(app, chatid, "Select the PasarGuard role. Owner cannot be assigned through this API.", inline(rows))


async def confirm(app, chatid, action, text, **context):
    context.update(mode="confirm", nonce=secrets.token_hex(4), action=action)
    db.set_state(chatid, context)
    await say(app, chatid, text, inline([[("Confirm", f"confirm|{context['nonce']}"), ("Cancel", "home")]]))


def batch_names(name, count):
    if count == 1:
        return [username(name)]
    match = re.fullmatch(r"(.*?)(\d+)", name)
    base, first = (match[1], int(match[2])) if match else (name, 1)
    return [username(f"{base}{first + n}") for n in range(count)]


async def create_batch(app, chatid, context):
    names = batch_names(context["name"], context["count"])
    for name in names:
        create_payload(name, context["gb"], context["days"], context["groups"])
    db.set_state(chatid, {"mode": "busy"})
    created = 0
    try:
        for name in names:
            user = await panel(chatid, "create_user", name, context["gb"], context["days"], context["groups"])
            created += 1
            url = user.get("subscription_url")
            if not url:
                user = await panel(chatid, "user", user["id"])
                url = user.get("subscription_url")
            await say(app, chatid, f"✅ <b>{escape(name)}</b> · {context['gb']} GB · {context['days']} days")
            if url:
                qr = await asyncio.to_thread(DEF_CREATE_QRCODE, url)
                try:
                    await app.send_photo(chatid, qr)
                finally:
                    qr.close()
                await app.send_message(chatid, url, parse_mode=enums.ParseMode.DISABLED, disable_web_page_preview=True)
    finally:
        db.set_state(chatid)
        await say(
            app,
            chatid,
            f"Created {created}/{len(names)} users. Check the panel before retrying an interrupted batch.",
            HOME,
        )


async def show_user(app, chatid, value):
    try:
        user = await panel(chatid, "find_user", value)
    except PanelError as exc:
        if exc.status != 404:
            raise
        names = await panel_search(chatid, value)
        await say(app, chatid, names, HOME)
        return
    update = await panel(chatid, "last_subscription_update", user["id"])
    keyboard = inline(
        [
            [("Refresh", f"u|{user['id']}"), ("QR / subscription", f"qr|{user['id']}")],
            [("Delete user", f"udel|{user['id']}")],
        ]
    )
    await say(app, chatid, user_text(user, update), keyboard)


async def panel_search(chatid, text):
    names = await asyncio.to_thread(find_similar, db.client_for(chatid), text)
    return "\n".join(f"<code>{escape(name)}</code>" for name in names[:20]) or "No matching users."


async def show_node(app, chatid, node_id):
    node = await panel(chatid, "node", node_id)
    text = (
        f"<b>{escape(node['name'])}</b> · {escape(node['status'])}\n"
        f"Address: {escape(node['address'])}\nUsage coefficient: {node['usage_coefficient']}"
    )
    await say(
        app,
        chatid,
        text,
        inline(
            [
                [("Enable", f"nenable|{node_id}"), ("Disable", f"ndisable|{node_id}")],
                [("Reconnect", f"nreconnect|{node_id}"), ("Usage coefficient", f"nusage|{node_id}")],
            ]
        ),
    )


async def show_admin(app, chatid, admin_id):
    admin = await panel(chatid, "admin", admin_id)
    role = admin.get("role") or {}
    await say(
        app,
        chatid,
        f"<b>{escape(admin['username'])}</b>\nRole: {escape(role.get('name', '—'))}\nState: {escape(admin['status'])}",
        inline(
            [
                [("Change password", f"apass|{admin_id}"), ("Change role", f"arole|{admin_id}")],
                [("Delete admin", f"adel|{admin_id}")],
            ]
        ),
    )


async def show_monitoring(app, chatid):
    config = db.monitoring(chatid)
    await say(
        app,
        chatid,
        f"Monitoring: <b>{config['status']}</b>\nNormal: {config['check_normal']}s · Error: {config['check_error']}s",
        inline(
            [
                [("Toggle monitoring", "mtoggle")],
                [("Normal interval", "mtimer|check_normal"), ("Error interval", "mtimer|check_error")],
            ]
        ),
    )


async def show_notice(app, chatid):
    await say(
        app,
        chatid,
        "<b>PasarGuard notices</b>\n"
        "Subscription announcements apply to all subscriptions and are displayed by supported clients (Happ / v2RayTun).\n"
        "Expiry alerts notify you when an account becomes expired or limited.\n"
        f"Admin expiry alerts: {'on' if db.alerts_enabled(chatid) else 'off'}",
        inline(
            [
                [("View announcement", "announceview"), ("Edit announcement", "announceedit")],
                [("Toggle admin expiry alerts", "alerttoggle")],
            ]
        ),
    )


async def handle_message(app, message):
    chatid = message.chat.id
    if not message.from_user or message.from_user.id != chatid or not db.authorized(chatid):
        return
    text = message.text or message.caption
    if not text:
        return
    try:
        mode = db.state(chatid).get("mode")
        await process_message(app, message, text if mode in {"admin_password", "change_password"} else text.strip())
    except (PanelError, ValueError) as exc:
        await say(app, chatid, escape(str(exc)), CANCEL)
    except sqlite3.IntegrityError:
        await say(app, chatid, "This template name already exists. Choose another name.", CANCEL)
    except Exception:
        log.exception("Message operation failed")
        await say(app, chatid, "The operation failed. Check the service log and panel state before retrying.", HOME)


async def process_message(app, message, text):
    chatid = message.chat.id
    if text in {"/start", "/cancel", "cancel", "🔙 cancel"}:
        db.set_state(chatid)
        await say(
            app,
            chatid,
            f"<b>PG-ToolBox</b> · PasarGuard {SUPPORTED_PANEL}\nSend a username or subscription URL to view an account.",
            HOME,
        )
        return
    # Home buttons work from every conversation state.
    if text == "🚀 Create User":
        db.set_state(chatid, {"mode": "template_list", "purpose": "create"})
        await resource_list(app, chatid, "t")
    elif text == "🗃 Templates":
        db.set_state(chatid, {"mode": "template_list", "purpose": "delete"})
        await resource_list(app, chatid, "t")
    elif text == "🎗 Nodes":
        db.set_state(chatid)
        await resource_list(app, chatid, "node")
    elif text == "👨🏻‍💻 Admins":
        db.set_state(chatid)
        await resource_list(app, chatid, "admin")
    elif text == "👤 Users":
        db.set_state(chatid, {"mode": "users"})
        await say(app, chatid, await asyncio.to_thread(summary, db.client_for(chatid)), USERS)
    elif text == "🎛 Monitoring":
        db.set_state(chatid)
        await show_monitoring(app, chatid)
    elif text == "🎖 Notice":
        db.set_state(chatid)
        await show_notice(app, chatid)
    elif text in {"🔍 Search", "🖼 QR Code"}:
        db.set_state(chatid, {"mode": "search" if text == "🔍 Search" else "qr"})
        await say(app, chatid, "Send search text." if text == "🔍 Search" else "Send the QR text / URL.", CANCEL)
    elif text == "💬 Help":
        await say(
            app,
            chatid,
            '<a href="https://github.com/DenTeeR/PG-ToolBox">PG-ToolBox documentation</a>\n'
            "For permission errors, check your PasarGuard role and API key scopes.\n"
            "Service log: <code>journalctl -u pg-toolbox</code>",
            HOME,
        )
    else:
        context = db.state(chatid)
        mode = context.get("mode")
        if mode == "users":
            if text in STATUS_BUTTONS:
                users = await panel(chatid, "users", status=STATUS_BUTTONS[text])
                await send_pdf(app, chatid, [user["username"] for user in users], STATUS_BUTTONS[text])
            elif text in {"👀 Online time list", "📡 Sub Update list"}:
                db.set_state(
                    chatid, {"mode": "time", "category": "online" if text.startswith("👀") else "subscription"}
                )
                await say(
                    app,
                    chatid,
                    "Enter a period, e.g. 15 min, 24 hour or 7 day. Subscription reports check each user's history and may take a while.",
                    TIMES,
                )
            elif text == "💻 Clients":
                segments = await panel(chatid, "clients")
                for start in range(0, len(segments), 5):
                    await say(
                        app,
                        chatid,
                        "\n".join(f"{escape(s['name'][:300])}: {s['count']}" for s in segments[start : start + 5]),
                    )
                if not segments:
                    await say(app, chatid, "No recorded subscription clients.")
        elif mode == "time":
            match = re.fullmatch(r"([0-9]+) (min|hour|day)", text)
            if not match:
                raise ValueError("Use a period such as 15 min, 24 hour or 7 day.")
            seconds = int(match[1]) * {"min": 60, "hour": 3600, "day": 86400}[match[2]]
            if not 0 < seconds <= 60 * 86400:
                raise ValueError("Choose a period between 1 minute and 60 days.")
            yes, no = await asyncio.to_thread(time_report, db.client_for(chatid), context["category"], seconds)
            await send_pdf(app, chatid, yes, f"{context['category']}: within {text}")
            await send_pdf(app, chatid, no, f"{context['category']}: older or never recorded")
        elif mode == "search":
            await say(app, chatid, await panel_search(chatid, text))
        elif mode == "qr":
            qr = await asyncio.to_thread(DEF_CREATE_QRCODE, text)
            try:
                await app.send_photo(chatid, qr)
            finally:
                qr.close()
            db.set_state(chatid)
            await say(app, chatid, "QR code created.", HOME)
        elif mode in {"name", "template_username"}:
            if mode == "name" and context["purpose"] == "template":
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}", text):
                    raise ValueError("Template name: 1–32 Latin letters/digits/underscores, starting with a letter.")
                if any(t["name"] == text for t in db.templates()):
                    raise ValueError("That template already exists.")
            else:
                username(text)
            context.update(name=text, mode="count" if mode == "template_username" else "gb")
            db.set_state(chatid, context)
            await say(
                app,
                chatid,
                "How many users? 1–500." if mode == "template_username" else "Traffic limit in GB (0 = unlimited)?",
                CANCEL,
            )
        elif mode == "gb":
            traffic_bytes(text)
            context.update(gb=text, mode="days")
            db.set_state(chatid, context)
            await say(app, chatid, "Validity in whole days from first connection (0 = unlimited)?", CANCEL)
        elif mode == "days":
            duration_seconds(text)
            context.update(days=int(text))
            if context["purpose"] == "template":
                await select_groups(app, chatid, context)
            else:
                context["mode"] = "count"
                db.set_state(chatid, context)
                await say(app, chatid, "How many users? 1–500.", CANCEL)
        elif mode == "count":
            if not text.isascii() or not text.isdigit() or not 1 <= int(text) <= 500:
                raise ValueError("Enter a whole number from 1 to 500.")
            context["count"] = int(text)
            batch_names(context["name"], context["count"])
            if context.get("groups"):
                await confirm_creation(app, chatid, context)
            else:
                await select_groups(app, chatid, context)
        elif mode == "admin_name":
            if not text or len(text) > 128:
                raise ValueError("Enter a valid admin username.")
            await select_role(app, chatid, {"purpose": "new_admin", "name": text})
        elif mode in {"admin_password", "change_password"}:
            try:
                if mode == "admin_password":
                    await panel(chatid, "create_admin", context["name"], text, context["role_id"])
                else:
                    updated = await panel(chatid, "update_admin", context["id"], password=text)
                    db.update_login_password(chatid, updated["username"], text)
            finally:
                try:
                    await message.delete()
                except RPCError:
                    pass
            db.set_state(chatid)
            await say(app, chatid, "Admin saved.", HOME)
        elif mode == "node_usage":
            try:
                value = float(text)
            except ValueError:
                raise ValueError("Enter a finite, nonnegative coefficient.") from None
            if not 0 <= value < float("inf"):
                raise ValueError("Enter a finite, nonnegative coefficient.")
            await panel(chatid, "update_node", context["id"], usage_coefficient=value)
            db.set_state(chatid)
            await show_node(app, chatid, context["id"])
        elif mode == "timer":
            if not text.isascii() or not text.isdigit() or not 10 <= int(text) <= 86400:
                raise ValueError("Enter an interval from 10 to 86400 seconds.")
            db.set_monitoring(chatid, **{context["field"]: int(text)})
            db.set_state(chatid)
            await show_monitoring(app, chatid)
        elif mode == "announcement":
            await panel(chatid, "set_announcement", "" if text == "/clear" else text)
            db.set_state(chatid)
            await say(app, chatid, "Subscription announcement saved.", HOME)
        elif mode:
            await say(app, chatid, "Use the current buttons or /cancel.", CANCEL)
        else:
            await show_user(app, chatid, text)


async def confirm_creation(app, chatid, context):
    context = {k: v for k, v in context.items() if k not in {"mode", "nonce", "options", "action"}}
    await confirm(
        app,
        chatid,
        "create",
        f"Create {context['count']} user(s), {context['gb']} GB, {context['days']} days, "
        f"groups {escape(str(context['groups']))}?",
        **context,
    )


async def handle_callback(app, query):
    if (
        not query.message
        or not query.from_user
        or query.message.chat.id != query.from_user.id
        or not db.authorized(query.from_user.id)
    ):
        await query.answer("Access denied.", show_alert=True)
        return
    chatid = query.from_user.id
    try:
        await query.answer()
        await process_callback(app, chatid, query.data.split("|"), query)
    except MessageNotModified:
        pass
    except (PanelError, ValueError) as exc:
        await say(app, chatid, escape(str(exc)), CANCEL)
    except Exception:
        log.exception("Callback operation failed")
        await say(app, chatid, "The operation failed. Check the service log and panel state before retrying.", HOME)


async def process_callback(app, chatid, data, query):
    command = data[0]
    context = db.state(chatid)
    if command == "home":
        db.set_state(chatid)
        await say(app, chatid, "PG-ToolBox", HOME)
    elif command == "nav":
        await resource_list(app, chatid, data[1], int(data[2]))
    elif command in {"g", "gp", "gs", "role", "confirm"}:
        expected_mode = {"g": "groups", "gp": "groups", "gs": "groups", "role": "role", "confirm": "confirm"}[command]
        if context.get("mode") != expected_mode or context.get("nonce") != data[1]:
            raise ValueError("This selection expired. Open the menu again.")
        if command == "g":
            group_id = int(data[2])
            if group_id not in {g["id"] for g in context["options"]}:
                raise ValueError("Unknown group.")
            groups = set(context["groups"])
            groups.symmetric_difference_update({group_id})
            context["groups"] = sorted(groups)
            db.set_state(chatid, context)
            await query.edit_message_reply_markup(group_keyboard(context, context.get("page", 0)))
        elif command == "gp":
            context["page"] = int(data[2])
            db.set_state(chatid, context)
            await query.edit_message_reply_markup(group_keyboard(context, context["page"]))
        elif command == "gs":
            if not context["groups"]:
                raise ValueError("Select at least one group.")
            if context["purpose"] == "template":
                db.save_template(context["name"], context["gb"], context["days"], context["groups"])
                db.set_state(chatid)
                await say(app, chatid, "Template saved.", HOME)
            else:
                await confirm_creation(app, chatid, context)
        elif command == "role":
            role_id = int(data[2])
            if role_id not in context["role_ids"]:
                raise ValueError("Unknown role.")
            if context["purpose"] == "new_admin":
                db.set_state(chatid, {"mode": "admin_password", "name": context["name"], "role_id": role_id})
                await say(
                    app,
                    chatid,
                    "Send the new password (12+ characters, two uppercase, two lowercase, two digits and a symbol).",
                    CANCEL,
                )
            else:
                await panel(chatid, "update_admin", context["id"], role_id=role_id)
                db.set_state(chatid)
                await say(app, chatid, "Admin role updated.", HOME)
        elif command == "confirm":
            # Consume before performing a mutation; a double-tap cannot repeat it.
            db.set_state(chatid)
            action = context["action"]
            if action == "create":
                await create_batch(app, chatid, context)
            elif action == "delete_user":
                await panel(chatid, "delete_user", context["id"])
                await say(app, chatid, "User deleted.", HOME)
            elif action == "delete_admin":
                await panel(chatid, "delete_admin", context["id"])
                await say(app, chatid, "Admin deleted.", HOME)
            elif action == "delete_template":
                db.delete_template(context["name"])
                await say(app, chatid, "Template deleted.", HOME)
    elif command in {"manual", "templatenew"}:
        db.set_state(chatid, {"mode": "name", "purpose": "create" if command == "manual" else "template"})
        await say(app, chatid, "Send the username." if command == "manual" else "Send a template name.", CANCEL)
    elif command == "t":
        if context.get("mode") != "template_list":
            raise ValueError("Open the template list again.")
        template = next((t for t in db.templates() if t["name"] == data[1]), None)
        if not template:
            raise ValueError("Template no longer exists.")
        if context["purpose"] == "create":
            db.set_state(
                chatid,
                {
                    "mode": "template_username",
                    "purpose": "create",
                    "gb": template["data"],
                    "days": template["days"],
                    "groups": template["group_ids"],
                },
            )
            await say(app, chatid, "Send the username / batch prefix.", CANCEL)
        else:
            await confirm(
                app, chatid, "delete_template", f"Delete template {escape(template['name'])}?", name=template["name"]
            )
    elif command == "node":
        await show_node(app, chatid, int(data[1]))
    elif command in {"nenable", "ndisable", "nreconnect", "nusage"}:
        node_id = int(data[1])
        if command == "nusage":
            db.set_state(chatid, {"mode": "node_usage", "id": node_id})
            await say(app, chatid, "Send the usage coefficient (e.g. 1 or 0.5).", CANCEL)
        elif command == "nreconnect":
            await panel(chatid, "reconnect_node", node_id)
            await say(app, chatid, "Reconnect requested. Refresh the node to verify its state.", HOME)
        else:
            await panel(chatid, "update_node", node_id, status="connected" if command == "nenable" else "disabled")
            await show_node(app, chatid, node_id)
    elif command == "admin":
        await show_admin(app, chatid, int(data[1]))
    elif command == "adminnew":
        db.set_state(chatid, {"mode": "admin_name"})
        await say(app, chatid, "Send the new admin username.", CANCEL)
    elif command == "apass":
        db.set_state(chatid, {"mode": "change_password", "id": int(data[1])})
        await say(
            app,
            chatid,
            "Send the new password (12+ characters, two uppercase, two lowercase, two digits and a symbol).",
            CANCEL,
        )
    elif command == "arole":
        await select_role(app, chatid, {"purpose": "change_role", "id": int(data[1])})
    elif command == "adel":
        await confirm(app, chatid, "delete_admin", "Delete this admin?", id=int(data[1]))
    elif command in {"u", "qr", "udel"}:
        user_id = int(data[1])
        user = await panel(chatid, "user", user_id)
        if command == "u":
            await show_user(app, chatid, user["username"])
        elif command == "udel":
            await confirm(app, chatid, "delete_user", f"Delete {escape(user['username'])}?", id=user_id)
        else:
            url = user.get("subscription_url")
            if not url:
                raise ValueError("The panel did not return a subscription URL.")
            qr = await asyncio.to_thread(DEF_CREATE_QRCODE, url)
            try:
                await app.send_photo(chatid, qr)
            finally:
                qr.close()
            await app.send_message(chatid, url, parse_mode=enums.ParseMode.DISABLED, disable_web_page_preview=True)
    elif command == "mtoggle":
        db.set_monitoring(chatid, status="off" if db.monitoring(chatid)["status"] == "on" else "on")
        await show_monitoring(app, chatid)
    elif command == "mtimer":
        if data[1] not in {"check_normal", "check_error"}:
            raise ValueError("Unknown interval.")
        db.set_state(chatid, {"mode": "timer", "field": data[1]})
        await say(app, chatid, "Send the interval in seconds (10–86400).", CANCEL)
    elif command == "announceview":
        text = await panel(chatid, "announcement")
        await say(app, chatid, escape(text) or "No subscription announcement configured.")
    elif command == "announceedit":
        db.set_state(chatid, {"mode": "announcement"})
        await say(
            app,
            chatid,
            "Send the announcement for ALL subscriptions (max 128 characters), or /clear to remove it.",
            CANCEL,
        )
    elif command == "alerttoggle":
        await asyncio.to_thread(db.toggle_alerts, chatid)
        await show_notice(app, chatid)


async def monitor_loop(app, chatid):
    while True:
        interval = 60
        try:
            messages, interval = await asyncio.to_thread(check_nodes, chatid)
            for text in messages:
                await say(app, chatid, text)
        except (PanelError, ValueError) as exc:
            log.warning("Node check failed: %s", exc)
        except Exception:
            log.exception("Node check failed")
        await asyncio.sleep(interval)


async def notice_loop(app, chatid):
    while True:
        try:
            pending = await asyncio.to_thread(pending_notices, chatid)
            for user_id, status, text in pending:
                await say(app, chatid, text)
                db.record_status(chatid, user_id, status)
        except (PanelError, ValueError) as exc:
            log.warning("Expiry check failed: %s", exc)
        except Exception:
            log.exception("Expiry check failed")
        await asyncio.sleep(30)


async def main():
    db.initialize()
    owner = db.config()["chatid"]
    app = make_client()
    # Serial handlers protect each confirmation/state transition from duplicate
    # callbacks. HTTP calls run in threads so background checks can still run.
    app.workers = 1
    app.on_message(filters.private)(handle_message)
    app.on_callback_query()(handle_callback)
    async with app:
        jobs = [asyncio.create_task(monitor_loop(app, owner)), asyncio.create_task(notice_loop(app, owner))]
        try:
            await idle()
        finally:
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(main())
