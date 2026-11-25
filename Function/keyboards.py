from pyrogram.types import InlineKeyboardButton as Button, InlineKeyboardMarkup, ReplyKeyboardMarkup

HOME = ReplyKeyboardMarkup(
    [
        ["🚀 Create User", "🎖 Notice"],
        ["👤 Users", "👨🏻‍💻 Admins", "🎗 Nodes"],
        ["🗃 Templates", "🎛 Monitoring"],
        ["🔍 Search", "💬 Help", "🖼 QR Code"],
    ],
    resize_keyboard=True,
)
CANCEL = ReplyKeyboardMarkup([["🔙 cancel"]], resize_keyboard=True)
USERS = ReplyKeyboardMarkup(
    [
        ["✅ Active", "❌ Disabled", "🕰 Expired", "🪫 Limited", "🔌 On Hold"],
        ["👀 Online time list", "📡 Sub Update list", "💻 Clients"],
        ["🔙 cancel"],
    ],
    resize_keyboard=True,
)
TIMES = ReplyKeyboardMarkup(
    [
        ["1 min", "15 min", "1 hour", "24 hour"],
        ["1 day", "7 day", "30 day", "60 day"],
        ["🔙 cancel"],
    ],
    resize_keyboard=True,
)


def inline(rows):
    return InlineKeyboardMarkup([[Button(label, callback_data=data) for label, data in row] for row in rows])


def paged(items, kind, label, page=0, extra=None):
    page = max(0, min(page, (max(1, len(items)) - 1) // 10))
    rows = [[(label(item), f"{kind}|{item['id']}")] for item in items[page * 10 : (page + 1) * 10]]
    navigation = []
    if page:
        navigation.append(("←", f"nav|{kind}|{page - 1}"))
    if (page + 1) * 10 < len(items):
        navigation.append(("→", f"nav|{kind}|{page + 1}"))
    if navigation:
        rows.append(navigation)
    if extra:
        rows.append(extra)
    return inline(rows or [[("Home", "home")]])


def group_keyboard(state, page=0):
    items = state["options"]
    nonce = state["nonce"]
    selected = set(state["groups"])
    page = max(0, min(page, (max(1, len(items)) - 1) // 10))
    rows = [
        [(f"{'✅' if item['id'] in selected else '▫️'} {item['name']}", f"g|{nonce}|{item['id']}")]
        for item in items[page * 10 : (page + 1) * 10]
    ]
    navigation = []
    if page:
        navigation.append(("←", f"gp|{nonce}|{page - 1}"))
    if (page + 1) * 10 < len(items):
        navigation.append(("→", f"gp|{nonce}|{page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.append([("Save selection", f"gs|{nonce}"), ("Cancel", "home")])
    return inline(rows)
