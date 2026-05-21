"""
DigiNeighbour Bot — Complete bot with:
- Invite-only access with tracking
- Spam caps (daily/weekly/monthly/total per category)
- Subscriptions with /unsubscribe_X links
- Group buying with voting
- Sponsorship banners
- /help /author /contact /invite commands
- Nested category menus
- Full listing flow with confirmation
"""
import asyncio, json, logging, os, sys, re, secrets
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand)
from telegram.ext import (Application, CommandHandler, MessageHandler,
                          CallbackQueryHandler, filters, ContextTypes)
from telegram.constants import ParseMode
from core.db import get_db, init_db, q, run

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler(os.environ.get("LOG_PATH", "/app/logs/bot.log"), encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("bot")

AUTHOR = "Samir"
AUTHOR_TG = "@designdazzle"
APP_NAME = "DigiNeighbour"
TAGLINE = "Your Smart Digital Neighbourhood"

# ── DB helpers ────────────────────────────────────────────────────────────────

def get_community(token: str) -> dict | None:
    return q("SELECT * FROM communities WHERE bot_token=? AND is_active=1", (token,), one=True)

def get_user(community_id: int, telegram_id: int) -> dict | None:
    return q("SELECT * FROM users WHERE community_id=? AND telegram_id=?",
             (community_id, telegram_id), one=True)

def upsert_user(community_id: int, tg_user) -> dict:
    existing = get_user(community_id, tg_user.id)
    if existing:
        run("UPDATE users SET last_active=datetime('now'),display_name=?,telegram_username=? WHERE id=?",
            (tg_user.full_name, tg_user.username, existing["id"]))
        return existing
    uid = run("INSERT INTO users(community_id,telegram_id,telegram_username,display_name,last_active) "
              "VALUES(?,?,?,?,datetime('now'))",
              (community_id, tg_user.id, tg_user.username, tg_user.full_name))
    return q("SELECT * FROM users WHERE id=?", (uid,), one=True)

def get_conv(tg_id: int, cid: int) -> dict:
    r = q("SELECT state,context FROM conversations WHERE telegram_id=? AND community_id=?",
          (tg_id, cid), one=True)
    return {"state": r["state"], "context": json.loads(r["context"] or "{}")} if r else {"state": "idle", "context": {}}

def save_conv(tg_id: int, cid: int, state: str, ctx: dict):
    run("INSERT INTO conversations(telegram_id,community_id,state,context,updated_at) VALUES(?,?,?,?,datetime('now')) "
        "ON CONFLICT(telegram_id,community_id) DO UPDATE SET state=excluded.state,context=excluded.context,updated_at=excluded.updated_at",
        (tg_id, cid, state, json.dumps(ctx)))

def reset_conv(tg_id: int, cid: int):
    save_conv(tg_id, cid, "idle", {})

# ── Invite system ─────────────────────────────────────────────────────────────

def generate_invite(community_id: int, created_by_user_id: int) -> str | None:
    """Generate a one-time invite code. Returns None if user hit their limit."""
    user = q("SELECT * FROM users WHERE id=?", (created_by_user_id,), one=True)
    community = q("SELECT * FROM communities WHERE id=?", (community_id,), one=True)
    max_invites = community.get("max_invites_per_user", 10)

    active_invites = q("SELECT COUNT(*) as n FROM invite_links WHERE created_by=? AND is_used=0 AND is_active=1",
                       (created_by_user_id,), one=True)["n"]
    total_generated = user.get("invites_generated", 0)

    if total_generated >= max_invites:
        return None

    code = secrets.token_urlsafe(12)
    run("INSERT INTO invite_links(community_id,code,created_by) VALUES(?,?,?)",
        (community_id, code, created_by_user_id))
    run("UPDATE users SET invites_generated=invites_generated+1 WHERE id=?", (created_by_user_id,))
    return code

def use_invite(community_id: int, code: str, user_id: int) -> bool:
    """Mark invite as used. Returns True if valid."""
    invite = q("SELECT * FROM invite_links WHERE code=? AND community_id=? AND is_used=0 AND is_active=1",
               (code, community_id), one=True)
    if not invite:
        return False
    run("UPDATE invite_links SET is_used=1,used_by=?,used_at=datetime('now') WHERE id=?",
        (user_id, invite["id"]))
    run("UPDATE users SET invite_code_used=?,invited_by=? WHERE id=?",
        (code, invite["created_by"], user_id))
    return True

# ── Spam caps ─────────────────────────────────────────────────────────────────

def check_listing_cap(user_id: int, category_id: int) -> tuple[bool, str]:
    """Returns (allowed, reason). Checks daily/weekly/monthly/total caps."""
    cat = q("SELECT * FROM categories WHERE id=?", (category_id,), one=True)
    if not cat:
        return True, ""

    checks = [
        (cat.get("max_listings_per_user_daily"), "day", "daily"),
        (cat.get("max_listings_per_user_weekly"), "7 days", "weekly"),
        (cat.get("max_listings_per_user_monthly"), "30 days", "monthly"),
    ]
    for limit, period, label in checks:
        if limit and limit > 0:
            if period == "day":
                count = q("SELECT COUNT(*) as n FROM listings WHERE user_id=? AND category_id=? AND date(created_at)=date('now')",
                          (user_id, category_id), one=True)["n"]
            else:
                days = 7 if period == "7 days" else 30
                count = q(f"SELECT COUNT(*) as n FROM listings WHERE user_id=? AND category_id=? AND created_at>=datetime('now','-{days} days')",
                          (user_id, category_id), one=True)["n"]
            if count >= limit:
                return False, f"You've reached your {label} limit of {limit} listing(s) in this category."

    total_limit = cat.get("max_listings_per_user_total")
    if total_limit and total_limit > 0:
        count = q("SELECT COUNT(*) as n FROM listings WHERE user_id=? AND category_id=? AND status='active'",
                  (user_id, category_id), one=True)["n"]
        if count >= total_limit:
            return False, f"You can have max {total_limit} active listing(s) in this category. Delete an old one first."

    return True, ""

# ── Sponsorship ───────────────────────────────────────────────────────────────

def get_sponsor_banner(community_id: int) -> str | None:
    """Get a sponsor banner to show (respects daily cap)."""
    today = datetime.now().strftime("%Y-%m-%d")
    sponsors = q("SELECT * FROM sponsorships WHERE community_id=? AND is_active=1 "
                 "AND start_date<=? AND end_date>=? ORDER BY RANDOM() LIMIT 1",
                 (community_id, today, today))
    if not sponsors:
        return None
    s = sponsors[0]
    # Check daily cap
    last_date = s.get("last_shown_date", "")
    today_count = s.get("today_show_count", 0) if last_date == today else 0
    if today_count >= (s.get("shows_per_day") or 3):
        return None
    # Update count
    run("UPDATE sponsorships SET show_count=show_count+1, today_show_count=?, last_shown_date=? WHERE id=?",
        (today_count + 1, today, s["id"]))
    banner = f"\n\n━━━━━━━━━━━━━━━━\n📣 *{s['sponsor_name']}*"
    if s.get("description"):
        banner += f"\n{s['description']}"
    if s.get("link"):
        banner += f"\n🔗 {s['link']}"
    banner += "\n━━━━━━━━━━━━━━━━"
    return banner

# ── Keyboards ─────────────────────────────────────────────────────────────────

def main_menu_kb(cid: int) -> InlineKeyboardMarkup:
    cats = q("SELECT * FROM categories WHERE community_id=? AND parent_id IS NULL AND is_active=1 ORDER BY sort_order,name", (cid,))
    btns = []
    row = []
    for i, c in enumerate(cats):
        row.append(InlineKeyboardButton(f"{c['icon']} {c['name']}", callback_data=f"cat:{c['id']}"))
        if len(row) == 2:
            btns.append(row); row = []
    if row:
        btns.append(row)
    btns.append([InlineKeyboardButton("🔍 Search", callback_data="search"),
                 InlineKeyboardButton("📋 My Listings", callback_data="mine")])
    btns.append([InlineKeyboardButton("🔔 Subscriptions", callback_data="subs"),
                 InlineKeyboardButton("🛒 Group Buying", callback_data="gb")])
    btns.append([InlineKeyboardButton("📣 Sponsorship", callback_data="sponsor_info"),
                 InlineKeyboardButton("❓ Help", callback_data="help")])
    return InlineKeyboardMarkup(btns)

def back_kb(cat_id=None) -> InlineKeyboardMarkup:
    btns = []
    if cat_id:
        btns.append(InlineKeyboardButton("⬅️ Back", callback_data=f"cat:{cat_id}"))
    btns.append(InlineKeyboardButton("🏠 Menu", callback_data="main"))
    return InlineKeyboardMarkup([btns])

# ── Send helper ───────────────────────────────────────────────────────────────

async def send(update: Update, text: str, kb=None, md=True):
    kwargs = {"parse_mode": ParseMode.MARKDOWN if md else None}
    if kb:
        kwargs["reply_markup"] = kb
    try:
        msg = update.callback_query.message if update.callback_query else update.message
        await msg.reply_text(text, **kwargs)
    except Exception:
        try:
            msg = update.callback_query.message if update.callback_query else update.message
            await msg.reply_text(text)
        except Exception as e:
            log.error(f"Send failed: {e}")

# ── Commands ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    community = get_community(ctx.bot.token)
    if not community:
        await send(update, "❌ Community not configured. Contact admin.")
        return

    cid = community["id"]
    tg_user = update.effective_user
    existing = get_user(cid, tg_user.id)

    # Check invite requirement
    if community.get("require_invite") and not existing:
        # Check if they sent an invite code as arg
        args = ctx.args or []
        code = args[0] if args else ""
        if not code:
            save_conv(tg_user.id, cid, "awaiting_invite", {})
            await send(update,
                f"👋 Welcome to *{community['name']}*!\n\n"
                f"This is a private community. You need an invite link to join.\n\n"
                f"Please paste your invite code:")
            return
        # Temp create user to use invite
        uid = run("INSERT OR IGNORE INTO users(community_id,telegram_id,telegram_username,display_name) VALUES(?,?,?,?)",
                  (cid, tg_user.id, tg_user.username, tg_user.full_name))
        user = get_user(cid, tg_user.id)
        if not use_invite(cid, code, user["id"]):
            await send(update, "❌ Invalid or already used invite link. Ask a community member for a new one.")
            return
        run("UPDATE users SET is_verified=1 WHERE id=?", (user["id"],))

    # Upsert user
    user = upsert_user(cid, tg_user)
    if user.get("is_banned"):
        await send(update, "🚫 Your account has been restricted. Contact admin.")
        return

    reset_conv(tg_user.id, cid)
    welcome = community.get("welcome_msg") or f"Welcome to *{community['name']}*!"
    banner = get_sponsor_banner(cid) or ""
    await send(update, f"{welcome}{banner}\n\nChoose from the menu:", kb=main_menu_kb(cid))

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = f"""❓ *{APP_NAME} Help*

*Listing something:*
Browse categories from the main menu, or use a command like /rent, /food, etc.

*Searching:*
/search \\[what you need\\] — or tap 🔍 Search in menu

*Your listings:*
Tap 📋 My Listings to manage them

*Subscriptions:*
Tap 🔔 in any category to get alerts when new listings appear.
To unsubscribe: /unsubscribe\\_CATEGORYID

*Invite a neighbour:*
/invite — generate a one-time invite link

*Group Buying:*
Tap 🛒 Group Buying to join collective purchases

*Sponsorship:*
/sponsor — advertise your business to the community

*Cancel anything:*
/cancel

_{APP_NAME} — {TAGLINE}_"""
    await send(update, text, kb=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="main")]]))

async def cmd_author(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = f"""👤 *About {APP_NAME}*

_{TAGLINE}_

Built by *{AUTHOR}*
Telegram: {AUTHOR_TG}

{APP_NAME} connects neighbours to share services, buy together, save money, and build a stronger community — all through Telegram.

🌟 Features:
• Listings for services, items, rentals, events
• Group buying with voting and rewards
• Subscription alerts
• Invite-only access

*Want this for your community?*
Contact {AUTHOR_TG}"""
    await send(update, text)

async def cmd_contact(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = f"""📬 *Contact & Ideas*

Have a suggestion? Want to add a feature? Interested in sponsorship? Want to add your community?

Contact *{AUTHOR}* at {AUTHOR_TG}

You can reach out for:
• 💡 New feature ideas
• 🏘️ Adding your community
• 📣 Sponsorship opportunities
• 🤝 Partnership enquiries
• 🐛 Bug reports

We read every message."""
    await send(update, text)

async def cmd_invite(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    community = get_community(ctx.bot.token)
    if not community:
        return
    cid = community["id"]
    user = get_user(cid, update.effective_user.id)
    if not user or user.get("is_banned"):
        await send(update, "You need to be a community member to generate invite links.")
        return

    code = generate_invite(cid, user["id"])
    if not code:
        max_inv = community.get("max_invites_per_user", 10)
        await send(update, f"❌ You have reached the maximum of {max_inv} invite links.\n\nYou can only generate {max_inv} invites total.")
        return

    bot_username = (await update.get_bot().get_me()).username
    link = f"https://t.me/{bot_username}?start={code}"
    remaining = community.get("max_invites_per_user", 10) - user.get("invites_generated", 0) - 1
    await send(update,
        f"🔗 *Your One-Time Invite Link*\n\n`{link}`\n\n"
        f"• Valid for one use only\n"
        f"• You are responsible for who you invite\n"
        f"• You have *{remaining}* invites remaining\n\n"
        f"Share this with a verified neighbour only!")

async def cmd_sponsor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    community = get_community(ctx.bot.token)
    if not community:
        return
    save_conv(update.effective_user.id, community["id"], "sponsor_inquiry", {})
    await send(update,
        f"📣 *Advertise on {APP_NAME}*\n\n"
        f"Reach verified residents of {community['name']} directly on Telegram.\n\n"
        f"Please tell us your *business name* to get started:")

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    community = get_community(ctx.bot.token)
    if not community:
        return
    reset_conv(update.effective_user.id, community["id"])
    await send(update, "✅ Cancelled.", kb=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="main")]]))

async def cmd_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    community = get_community(ctx.bot.token)
    if not community:
        return
    query = " ".join(ctx.args) if ctx.args else ""
    if not query:
        save_conv(update.effective_user.id, community["id"], "search", {})
        await send(update, "🔍 What are you looking for?")
        return
    await do_search(update, community, query)

# ── Search ────────────────────────────────────────────────────────────────────

async def do_search(update: Update, community: dict, query: str):
    cid = community["id"]
    results = []
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT l.*,c.name as cat_name,c.schema_fields,u.display_name,u.telegram_username "
                "FROM listings_fts f JOIN listings l ON l.id=f.rowid "
                "JOIN categories c ON c.id=l.category_id "
                "JOIN users u ON u.id=l.user_id "
                "WHERE listings_fts MATCH ? AND l.community_id=? AND l.status='active' LIMIT 10",
                (query, cid)
            ).fetchall()
            results = [dict(r) for r in rows]
    except Exception:
        pass
    if not results:
        results = q("SELECT l.*,c.name as cat_name,c.schema_fields,u.display_name,u.telegram_username "
                    "FROM listings l JOIN categories c ON c.id=l.category_id "
                    "JOIN users u ON u.id=l.user_id "
                    "WHERE l.community_id=? AND l.status='active' AND (l.title LIKE ? OR l.data LIKE ?) "
                    "ORDER BY l.created_at DESC LIMIT 10",
                    (cid, f"%{query}%", f"%{query}%"))
    if not results:
        await send(update, f"🔍 No results for *{query}*\n\nTry different keywords.",
                   kb=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="main")]]))
        return
    banner = get_sponsor_banner(cid) or ""
    lines = [f"🔍 *{len(results)} result(s) for \"{query}\"*\n"]
    for r in results:
        fields = json.loads(r.get("schema_fields") or "[]")
        data = json.loads(r.get("data") or "{}")
        contact = f"@{r['telegram_username']}" if r.get("telegram_username") else r.get("display_name", "")
        lines.append(f"• *{r['title']}* — {r['cat_name']}")
        for f in fields[:2]:
            if data.get(f["key"]):
                lines.append(f"  {f['label']}: {data[f['key']]}")
        lines.append(f"  👤 {contact} · /view\\_{r['id']}")
        lines.append("")
    await send(update, "\n".join(lines) + banner,
               kb=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="main")]]))

# ── Callbacks ─────────────────────────────────────────────────────────────────

async def handle_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q_obj = update.callback_query
    await q_obj.answer()
    data = q_obj.data
    community = get_community(ctx.bot.token)
    if not community:
        return
    cid = community["id"]
    tg_user = update.effective_user
    user = upsert_user(cid, tg_user)
    uid = user["id"]

    if data == "main":
        reset_conv(tg_user.id, cid)
        banner = get_sponsor_banner(cid) or ""
        await send(update, f"🏠 *Main Menu*{banner}", kb=main_menu_kb(cid))

    elif data == "help":
        await cmd_help(update, ctx)

    elif data == "search":
        save_conv(tg_user.id, cid, "search", {})
        await send(update, "🔍 Type what you're looking for:")

    elif data == "sponsor_info":
        save_conv(tg_user.id, cid, "sponsor_inquiry", {})
        await send(update,
            f"📣 *Advertise on {APP_NAME}*\n\n"
            f"Reach verified residents of *{community['name']}* directly.\n\n"
            f"Type your *business name* to begin:")

    elif data.startswith("cat:"):
        cat_id = int(data.split(":")[1])
        await show_category(update, community, cat_id, uid, tg_user.id)

    elif data.startswith("post:"):
        cat_id = int(data.split(":")[1])
        await start_posting(update, tg_user.id, cid, uid, cat_id)

    elif data.startswith("sub:"):
        cat_id = int(data.split(":")[1])
        cat = q("SELECT * FROM categories WHERE id=?", (cat_id,), one=True)
        try:
            run("INSERT INTO subscriptions(user_id,category_id) VALUES(?,?)", (uid, cat_id))
            listings = q("SELECT l.*,u.display_name,u.telegram_username FROM listings l "
                         "JOIN users u ON u.id=l.user_id "
                         "WHERE l.category_id=? AND l.status='active' ORDER BY l.created_at DESC LIMIT 8", (cat_id,))
            msg = f"✅ *Subscribed to {cat['name']}!*\n\nYou'll be notified of new listings.\n"
            if listings:
                msg += f"\n*Current listings ({len(listings)}):*\n"
                for l in listings:
                    contact = f"@{l['telegram_username']}" if l.get("telegram_username") else l.get("display_name", "")
                    msg += f"• {l['title']} — {contact} · /view\\_{l['id']}\n"
            msg += f"\nTo stop: /unsubscribe\\_{cat_id}"
        except Exception:
            msg = f"✅ Already subscribed to *{cat['name']}*\nTo stop: /unsubscribe\\_{cat_id}"
        await send(update, msg, kb=back_kb(cat_id))

    elif data.startswith("unsub:"):
        cat_id = int(data.split(":")[1])
        cat = q("SELECT name FROM categories WHERE id=?", (cat_id,), one=True)
        run("DELETE FROM subscriptions WHERE user_id=? AND category_id=?", (uid, cat_id))
        await send(update, f"🔕 Unsubscribed from *{cat['name'] if cat else str(cat_id)}*.")

    elif data == "mine":
        await show_my_listings(update, uid, cid)

    elif data == "subs":
        await show_my_subs(update, uid, cid)

    elif data == "gb":
        await show_group_buys(update, cid, uid)

    elif data.startswith("gb_join:"):
        gb_id = int(data.split(":")[1])
        await join_group_buy(update, gb_id, uid, tg_user.id, cid)

    elif data.startswith("gb_vote:"):
        _, gb_id, entry_id = data.split(":")
        await cast_vote(update, int(gb_id), int(entry_id), uid)

    elif data.startswith("del:"):
        lid = int(data.split(":")[1])
        listing = q("SELECT * FROM listings WHERE id=? AND user_id=?", (lid, uid), one=True)
        if listing:
            run("UPDATE listings SET status='archived' WHERE id=?", (lid,))
            await send(update, f"✅ Listing #{lid} removed.", kb=back_kb())
        else:
            await send(update, "Not found or not yours.")

# ── Category display ──────────────────────────────────────────────────────────

async def show_category(update: Update, community: dict, cat_id: int, uid: int, tg_id: int):
    cat = q("SELECT * FROM categories WHERE id=? AND is_active=1", (cat_id,), one=True)
    if not cat:
        return
    cid = community["id"]

    # Check dates
    today = datetime.now().strftime("%Y-%m-%d")
    if cat.get("start_date") and today < cat["start_date"]:
        await send(update, f"📅 *{cat['name']}* opens on {cat['start_date']}.", kb=back_kb(cat.get("parent_id")))
        return
    if cat.get("end_date") and today > cat["end_date"]:
        await send(update, f"📅 *{cat['name']}* closed on {cat['end_date']}.", kb=back_kb(cat.get("parent_id")))
        return

    # Sub-categories?
    subs = q("SELECT * FROM categories WHERE parent_id=? AND is_active=1 ORDER BY sort_order,name", (cat_id,))
    if subs:
        btns = []
        row = []
        for s in subs:
            row.append(InlineKeyboardButton(f"{s['icon']} {s['name']}", callback_data=f"cat:{s['id']}"))
            if len(row) == 2:
                btns.append(row); row = []
        if row:
            btns.append(row)
        nav = [InlineKeyboardButton("🏠 Menu", callback_data="main")]
        if cat.get("parent_id"):
            nav.insert(0, InlineKeyboardButton("⬅️ Back", callback_data=f"cat:{cat['parent_id']}"))
        btns.append(nav)
        await send(update, f"{cat['icon']} *{cat['name']}*\n_{cat.get('description','Choose a sub-category:')}_",
                   kb=InlineKeyboardMarkup(btns))
        return

    # Listings
    listings = q("SELECT l.*,u.display_name,u.telegram_username FROM listings l "
                 "JOIN users u ON u.id=l.user_id "
                 "WHERE l.category_id=? AND l.status='active' ORDER BY l.created_at DESC LIMIT 12", (cat_id,))

    btns = [
        [InlineKeyboardButton("➕ Post here", callback_data=f"post:{cat_id}"),
         InlineKeyboardButton("🔔 Subscribe", callback_data=f"sub:{cat_id}")],
    ]
    nav = [InlineKeyboardButton("🏠 Menu", callback_data="main")]
    if cat.get("parent_id"):
        nav.insert(0, InlineKeyboardButton("⬅️ Back", callback_data=f"cat:{cat['parent_id']}"))
    btns.append(nav)

    banner = get_sponsor_banner(cid) or ""
    if not listings:
        await send(update, f"{cat['icon']} *{cat['name']}*\n\nNo listings yet. Be the first!{banner}",
                   kb=InlineKeyboardMarkup(btns))
        return

    lines = [f"{cat['icon']} *{cat['name']}* — {len(listings)} listing(s)\n"]
    for l in listings:
        contact = f"@{l['telegram_username']}" if l.get("telegram_username") else l.get("display_name", "")
        lines.append(f"• *{l['title']}* — {contact}")
        lines.append(f"  /view\\_{l['id']}")
    await send(update, "\n".join(lines) + banner, kb=InlineKeyboardMarkup(btns))

async def show_my_listings(update: Update, uid: int, cid: int):
    listings = q("SELECT l.*,c.name as cat_name FROM listings l "
                 "JOIN categories c ON c.id=l.category_id "
                 "WHERE l.user_id=? AND l.community_id=? ORDER BY l.created_at DESC LIMIT 20", (uid, cid))
    if not listings:
        await send(update, "📋 You have no listings yet.", kb=back_kb())
        return
    lines = ["📋 *Your Listings*\n"]
    icons = {"active": "✅", "pending": "⏳", "archived": "📦"}
    btns = []
    for l in listings:
        icon = icons.get(l["status"], "❓")
        lines.append(f"{icon} *{l['title']}* ({l['cat_name']})")
        lines.append(f"   /view\\_{l['id']}")
        btns.append([InlineKeyboardButton(f"🗑 Delete: {l['title'][:30]}", callback_data=f"del:{l['id']}")])
    btns.append([InlineKeyboardButton("🏠 Menu", callback_data="main")])
    await send(update, "\n".join(lines), kb=InlineKeyboardMarkup(btns))

async def show_my_subs(update: Update, uid: int, cid: int):
    subs = q("SELECT c.* FROM subscriptions s JOIN categories c ON c.id=s.category_id WHERE s.user_id=?", (uid,))
    if not subs:
        await send(update, "🔔 No subscriptions.\n\nBrowse categories and tap Subscribe.",
                   kb=back_kb())
        return
    lines = ["🔔 *Your Subscriptions*\n"]
    btns = []
    for s in subs:
        lines.append(f"• {s['icon']} {s['name']}")
        btns.append([InlineKeyboardButton(f"❌ Unsubscribe: {s['name']}", callback_data=f"unsub:{s['id']}")])
    btns.append([InlineKeyboardButton("🏠 Menu", callback_data="main")])
    await send(update, "\n".join(lines), kb=InlineKeyboardMarkup(btns))

# ── Group Buying ──────────────────────────────────────────────────────────────

async def show_group_buys(update: Update, cid: int, uid: int):
    gbs = q("SELECT * FROM group_buys WHERE community_id=? AND status='open' ORDER BY created_at DESC LIMIT 10", (cid,))
    if not gbs:
        await send(update, "🛒 *Group Buying*\n\nNo active group buys right now.\nContact admin to create one.",
                   kb=back_kb())
        return
    lines = ["🛒 *Group Buying*\n"]
    btns = []
    for gb in gbs:
        e = q("SELECT COUNT(*) as cnt, SUM(tickets) as tot FROM group_buy_entries WHERE group_buy_id=?", (gb["id"],), one=True)
        total = (e.get("tot") or 0) * (gb.get("ticket_price") or 0)
        threshold = gb.get("threshold_amount") or 0
        pct = min(100, int(total / threshold * 100)) if threshold > 0 else 0
        lines.append(f"*{gb['title']}* (#{gb['id']})")
        if gb.get("ticket_price"):
            lines.append(f"  🎫 ₹{gb['ticket_price']:,.0f}/ticket · {e.get('cnt',0)} joined")
        if threshold > 0:
            lines.append(f"  📊 {pct}% of ₹{threshold:,.0f} threshold")
        if gb.get("expires_at"):
            lines.append(f"  ⌛ Closes: {gb['expires_at'][:10]}")
        lines.append("")
        btns.append([InlineKeyboardButton(f"Join: {gb['title'][:35]}", callback_data=f"gb_join:{gb['id']}")])
    btns.append([InlineKeyboardButton("🏠 Menu", callback_data="main")])
    await send(update, "\n".join(lines), kb=InlineKeyboardMarkup(btns))

async def join_group_buy(update: Update, gb_id: int, uid: int, tg_id: int, cid: int):
    gb = q("SELECT * FROM group_buys WHERE id=? AND status='open'", (gb_id,), one=True)
    if not gb:
        await send(update, "This group buy is no longer active."); return
    existing = q("SELECT * FROM group_buy_entries WHERE group_buy_id=? AND user_id=?", (gb_id, uid), one=True)
    if existing:
        adv = existing["tickets"] * (gb.get("ticket_price") or 0) * (gb.get("advance_percent") or 20) / 100
        msg = (f"✅ Already joined *{gb['title']}*\n\n"
               f"🎫 Your tickets: {existing['tickets']}\n"
               f"💳 Advance: ₹{adv:,.0f}")
        if gb.get("telegram_group_link"):
            msg += f"\n\n🔗 Group: {gb['telegram_group_link']}"
        await send(update, msg, kb=back_kb()); return
    save_conv(tg_id, cid, "gb_tickets", {"gb_id": gb_id})
    max_t = gb.get("max_tickets_per_user") or 10
    await send(update,
        f"🛒 *{gb['title']}*\n\n"
        f"🎫 Ticket price: ₹{gb.get('ticket_price',0):,.0f}\n"
        f"💳 Advance (20%): ₹{(gb.get('ticket_price',0)*0.2):,.0f} per ticket\n"
        f"Max per person: {max_t}\n\n"
        f"How many tickets do you want? (1–{max_t})")

async def cast_vote(update: Update, gb_id: int, entry_id: int, uid: int):
    existing = q("SELECT id FROM votes WHERE group_buy_id=? AND voter_id=?", (gb_id, uid), one=True)
    if existing:
        await send(update, "You've already voted."); return
    run("INSERT INTO votes(group_buy_id,voter_id,entry_id) VALUES(?,?,?)", (gb_id, uid, entry_id))
    run("UPDATE group_buy_entries SET votes_received=votes_received+1 WHERE id=?", (entry_id,))
    e = q("SELECT gbe.*,u.display_name FROM group_buy_entries gbe JOIN users u ON u.id=gbe.user_id WHERE gbe.id=?", (entry_id,), one=True)
    await send(update, f"✅ Voted for *{e['display_name']}*'s quote!\n₹{e.get('quote_amount',0):,.0f}")

# ── Posting flow ──────────────────────────────────────────────────────────────

async def start_posting(update: Update, tg_id: int, cid: int, uid: int, cat_id: int):
    cat = q("SELECT * FROM categories WHERE id=? AND is_active=1", (cat_id,), one=True)
    if not cat:
        await send(update, "Category not found."); return
    fields = json.loads(cat.get("schema_fields") or "[]")
    if not fields:
        await send(update, "This category has no fields. Contact admin."); return
    # Spam check
    ok, reason = check_listing_cap(uid, cat_id)
    if not ok:
        await send(update, f"⚠️ {reason}", kb=back_kb(cat_id)); return
    ctx = {"cat_id": cat_id, "fields": fields, "collected": {}, "idx": 0}
    save_conv(tg_id, cid, "posting", ctx)
    first = fields[0]
    reply = f"{cat['icon']} *New {cat['name']} listing*\n\n(1/{len(fields)}) {first['label']}"
    if first.get("example"):
        reply += f"\n\n💡 e.g. {first['example']}"
    if first.get("choices"):
        reply += "\n\n" + "\n".join(f"  {i+1}. {c}" for i,c in enumerate(first["choices"]))
    reply += "\n\n_/cancel to stop_"
    await send(update, reply)

# ── Message handler ───────────────────────────────────────────────────────────

async def handle_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    community = get_community(ctx.bot.token)
    if not community:
        return
    cid = community["id"]
    tg_user = update.effective_user

    # /view_ID
    if re.match(r"^/view_\d+$", text):
        lid = int(text.split("_")[1])
        listing = q("SELECT l.*,c.name as cat_name,c.schema_fields,c.id as cid_,"
                    "u.display_name,u.telegram_username,u.telegram_id "
                    "FROM listings l JOIN categories c ON c.id=l.category_id "
                    "JOIN users u ON u.id=l.user_id WHERE l.id=? AND l.community_id=?",
                    (lid, cid), one=True)
        if not listing:
            await send(update, "Listing not found."); return
        run("UPDATE listings SET view_count=view_count+1 WHERE id=?", (lid,))
        fields = json.loads(listing.get("schema_fields") or "[]")
        data = json.loads(listing.get("data") or "{}")
        contact = f"@{listing['telegram_username']}" if listing.get("telegram_username") else listing.get("display_name","")
        lines = [f"*{listing['title']}*", f"_{listing['cat_name']}_", ""]
        for f in fields:
            if data.get(f["key"]):
                lines.append(f"*{f['label']}:* {data[f['key']]}")
        lines += ["", f"👤 Contact: {contact}", f"📅 Posted: {listing['created_at'][:10]}"]
        if listing.get("expires_at"):
            lines.append(f"⌛ Valid until: {listing['expires_at'][:10]}")
        banner = get_sponsor_banner(cid) or ""
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔔 Subscribe", callback_data=f"sub:{listing['cid_']}"),
            InlineKeyboardButton("🏠 Menu", callback_data="main")
        ]])
        await send(update, "\n".join(lines) + banner, kb=kb)
        return

    # /unsubscribe_ID
    if re.match(r"^/unsubscribe_\d+$", text):
        cat_id = int(text.split("_")[1])
        user = get_user(cid, tg_user.id)
        if user:
            cat = q("SELECT name FROM categories WHERE id=?", (cat_id,), one=True)
            run("DELETE FROM subscriptions WHERE user_id=? AND category_id=?", (user["id"], cat_id))
            await send(update, f"🔕 Unsubscribed from *{cat['name'] if cat else str(cat_id)}*.")
        return

    # /delete_ID
    if re.match(r"^/delete_\d+$", text):
        user = get_user(cid, tg_user.id)
        if user:
            lid = int(text.split("_")[1])
            listing = q("SELECT id FROM listings WHERE id=? AND user_id=?", (lid, user["id"]), one=True)
            if listing:
                run("UPDATE listings SET status='archived' WHERE id=?", (lid,))
                await send(update, f"✅ Listing #{lid} removed.")
            else:
                await send(update, "Not found or not yours.")
        return

    # Category shortcuts /command
    if re.match(r"^/[a-z_]+$", text) and text not in ("/start","/help","/cancel","/search","/invite","/author","/contact","/sponsor"):
        cmd = text[1:].lower()
        cat = q("SELECT id FROM categories WHERE community_id=? AND command=? AND is_active=1", (cid, cmd), one=True)
        if cat:
            user = upsert_user(cid, tg_user)
            await show_category(update, community, cat["id"], user["id"], tg_user.id)
            return

    # State machine
    conv = get_conv(tg_user.id, cid)
    state = conv["state"]
    context = conv["context"]

    if state == "awaiting_invite":
        code = text.strip()
        uid = run("INSERT OR IGNORE INTO users(community_id,telegram_id,telegram_username,display_name) VALUES(?,?,?,?)",
                  (cid, tg_user.id, tg_user.username, tg_user.full_name))
        user = get_user(cid, tg_user.id)
        if use_invite(cid, code, user["id"]):
            run("UPDATE users SET is_verified=1 WHERE id=?", (user["id"],))
            reset_conv(tg_user.id, cid)
            welcome = community.get("welcome_msg") or f"Welcome to *{community['name']}*!"
            await send(update, f"✅ Invite accepted!\n\n{welcome}", kb=main_menu_kb(cid))
        else:
            await send(update, "❌ Invalid or expired invite code. Ask your neighbour for a new link.")
        return

    if state == "search":
        reset_conv(tg_user.id, cid)
        await do_search(update, community, text)
        return

    if state == "gb_tickets":
        gb_id = context.get("gb_id")
        gb = q("SELECT * FROM group_buys WHERE id=?", (gb_id,), one=True)
        if not gb:
            reset_conv(tg_user.id, cid); return
        user = get_user(cid, tg_user.id)
        try:
            tickets = int(text.strip())
            max_t = gb.get("max_tickets_per_user") or 10
            if tickets < 1 or tickets > max_t:
                await send(update, f"Enter a number 1–{max_t}."); return
            amt = tickets * (gb.get("ticket_price") or 0)
            adv = amt * (gb.get("advance_percent") or 20) / 100
            run("INSERT OR REPLACE INTO group_buy_entries(group_buy_id,user_id,tickets,advance_amount) VALUES(?,?,?,?)",
                (gb_id, user["id"], tickets, adv))
            reset_conv(tg_user.id, cid)
            msg = (f"✅ Joined *{gb['title']}*!\n\n"
                   f"🎫 Tickets: {tickets} · Total: ₹{amt:,.0f}\n"
                   f"💳 Advance: ₹{adv:,.0f}\n\n"
                   f"Pay advance via UPI to the community coordinator.")
            if gb.get("telegram_group_link"):
                msg += f"\n\n🔗 Group: {gb['telegram_group_link']}"
            await send(update, msg, kb=back_kb())
        except ValueError:
            await send(update, "Please enter a valid number.")
        return

    if state == "sponsor_inquiry":
        step = context.get("step", 0)
        if step == 0:
            context = {"step": 1, "business": text}
            save_conv(tg_user.id, cid, "sponsor_inquiry", context)
            await send(update, f"Great! Tell us about *{text}* — what do you offer?")
        elif step == 1:
            context["description"] = text
            context["step"] = 2
            save_conv(tg_user.id, cid, "sponsor_inquiry", context)
            await send(update, "Your contact number or email?")
        elif step == 2:
            run("INSERT INTO sponsorship_inquiries(community_id,telegram_id,telegram_username,name,business,message) VALUES(?,?,?,?,?,?)",
                (cid, tg_user.id, tg_user.username, tg_user.full_name, context.get("business"), f"{context.get('description')} | Contact: {text}"))
            reset_conv(tg_user.id, cid)
            await send(update,
                f"✅ *Enquiry received!*\n\n"
                f"*{AUTHOR}* will contact you at {AUTHOR_TG}\n\n"
                f"Thank you for your interest in sponsoring *{community['name']}*!",
                kb=back_kb())
        return

    if state == "posting":
        fields = context["fields"]
        idx = context["idx"]
        field = fields[idx]
        msg = text.strip()

        # Choice fields
        if field.get("choices"):
            choices = field["choices"]
            if msg.isdigit() and 1 <= int(msg) <= len(choices):
                msg = choices[int(msg)-1]
        context["collected"][field["key"]] = msg
        idx += 1
        context["idx"] = idx

        if idx < len(fields):
            nf = fields[idx]
            save_conv(tg_user.id, cid, "posting", context)
            reply = f"✓ {msg}\n\n({idx}/{len(fields)}) {nf['label']}"
            if nf.get("example"):
                reply += f"\n\n💡 e.g. {nf['example']}"
            if nf.get("choices"):
                reply += "\n\n" + "\n".join(f"  {i+1}. {c}" for i,c in enumerate(nf["choices"]))
            await send(update, reply); return

        # Confirm
        cat = q("SELECT * FROM categories WHERE id=?", (context["cat_id"],), one=True)
        title = context["collected"].get(fields[0]["key"], "Listing")[:100]
        lines = ["*Confirm your listing:*\n"]
        for f in fields:
            v = context["collected"].get(f["key"])
            if v:
                lines.append(f"*{f['label']}:* {v}")
        lines.append("\nReply *YES* to post, *EDIT* to restart, *CANCEL* to discard.")
        context["title"] = title
        save_conv(tg_user.id, cid, "confirming", context)
        await send(update, "\n".join(lines)); return

    if state == "confirming":
        context = conv["context"]
        user = get_user(cid, tg_user.id)
        msg_u = text.upper().strip()

        if msg_u in ("YES","Y","CONFIRM","POST","OK"):
            cat = q("SELECT * FROM categories WHERE id=?", (context["cat_id"],), one=True)
            # Re-check cap
            ok, reason = check_listing_cap(user["id"], context["cat_id"])
            if not ok:
                reset_conv(tg_user.id, cid)
                await send(update, f"⚠️ {reason}"); return

            title = context.get("title","Listing")
            data_j = json.dumps(context["collected"], ensure_ascii=False)
            expires_at = None
            if cat and cat.get("listing_days") and cat["listing_days"] > 0:
                expires_at = (datetime.now() + timedelta(days=cat["listing_days"])).strftime("%Y-%m-%d %H:%M:%S")
            needs_approval = cat and cat.get("requires_approval")
            status = "pending" if needs_approval else "active"
            lid = run("INSERT INTO listings(community_id,category_id,user_id,title,data,status,expires_at) VALUES(?,?,?,?,?,?,?)",
                      (cid, context["cat_id"], user["id"], title, data_j, status, expires_at))
            if needs_approval:
                run("INSERT INTO approval_queue(listing_id,submitted_by) VALUES(?,?)", (lid, user["id"]))
            reset_conv(tg_user.id, cid)

            msg = f"✅ *{'Submitted for approval' if needs_approval else 'Listing live!'}* (#{lid})\n\n"
            if expires_at:
                msg += f"Valid until: {expires_at[:10]}\n"
            msg += f"/view\\_{lid} · /delete\\_{lid}"

            # Notify subscribers
            if status == "active":
                subs = q("SELECT u.telegram_id FROM subscriptions s JOIN users u ON u.id=s.user_id "
                         "WHERE s.category_id=? AND u.telegram_id!=? AND u.is_banned=0", (context["cat_id"], tg_user.id))
                contact = f"@{tg_user.username}" if tg_user.username else tg_user.full_name
                notif = (f"🔔 *New listing in {cat['name']}*\n\n"
                         f"*{title}*\n"
                         f"by {contact}\n\n"
                         f"/view\\_{lid}\n\n"
                         f"_To unsubscribe: /unsubscribe\\_{context['cat_id']}_")
                for sub in subs:
                    try:
                        await ctx.bot.send_message(sub["telegram_id"], notif, parse_mode=ParseMode.MARKDOWN)
                    except Exception:
                        pass

            await send(update, msg, kb=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="main")]]))

        elif msg_u in ("EDIT","CHANGE"):
            user = get_user(cid, tg_user.id)
            await start_posting(update, tg_user.id, cid, user["id"], context["cat_id"])
        elif msg_u in ("CANCEL","NO"):
            reset_conv(tg_user.id, cid)
            await send(update, "❌ Cancelled.", kb=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="main")]]))
        else:
            await send(update, "Reply *YES*, *EDIT*, or *CANCEL*.")
        return

    # Default
    user = upsert_user(cid, tg_user)
    if user.get("is_banned"):
        await send(update, "🚫 Restricted. Contact admin."); return
    if community.get("require_invite") and not user.get("is_verified"):
        save_conv(tg_user.id, cid, "awaiting_invite", {})
        await send(update, "Please enter your invite code to join:")
        return
    banner = get_sponsor_banner(cid) or ""
    await send(update, f"🏠 *Main Menu*{banner}", kb=main_menu_kb(cid))

# ── Startup ───────────────────────────────────────────────────────────────────

async def post_init(app: Application):
    community = get_community(app.bot.token)
    if not community:
        return
    cats = q("SELECT command,name FROM categories WHERE community_id=? AND command IS NOT NULL AND is_active=1",
             (community["id"],))
    commands = [
        BotCommand("start", "Main menu"),
        BotCommand("menu", "Main menu"),
        BotCommand("search", "Search listings"),
        BotCommand("invite", "Generate invite link"),
        BotCommand("help", "Help & commands"),
        BotCommand("author", f"About {APP_NAME}"),
        BotCommand("contact", "Contact developer"),
        BotCommand("sponsor", "Advertise here"),
        BotCommand("cancel", "Cancel current action"),
    ]
    for c in cats:
        commands.append(BotCommand(c["command"], c["name"]))
    await app.bot.set_my_commands(commands)
    log.info(f"✅ Bot ready: {community['name']}")

def run_bot(token: str):
    os.makedirs(os.environ.get("LOG_DIR", "/app/logs"), exist_ok=True)
    log.info(f"Starting DigiNeighbour bot...")
    app = Application.builder().token(token).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("author", cmd_author))
    app.add_handler(CommandHandler("contact", cmd_contact))
    app.add_handler(CommandHandler("invite", cmd_invite))
    app.add_handler(CommandHandler("sponsor", cmd_sponsor))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(handle_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_msg))
    log.info("Bot polling started. Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=False)
