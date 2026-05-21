#!/usr/bin/env python3
"""DigiNeighbour Admin Dashboard"""
import os, sys, json, secrets
from datetime import datetime, timedelta
from functools import wraps

sys.path.insert(0, os.path.dirname(__file__))

from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, jsonify, g)
import bcrypt
from core.db import get_db, init_db, q, run, get_setting, set_setting

app = Flask(__name__, template_folder="dashboard/templates",
            static_folder="dashboard/static")
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.permanent_session_lifetime = timedelta(hours=12)

# ── Auth ──────────────────────────────────────────────────────────────────────

def get_admin():
    token = session.get("token")
    if not token:
        return None
    return q("SELECT s.*,a.username,a.display_name,a.role "
             "FROM admin_sessions s JOIN admins a ON a.id=s.admin_id "
             "WHERE s.token=? AND s.expires_at>datetime('now')", (token,), one=True)

def login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        admin = get_admin()
        if not admin:
            return redirect(url_for("login"))
        g.admin = admin
        return f(*args, **kwargs)
    return wrapped

def audit(action, details=None):
    try:
        admin = get_admin()
        run("INSERT INTO audit_log(admin_id,action,details,ip) VALUES(?,?,?,?)",
            (admin["admin_id"] if admin else None, action,
             json.dumps(details) if details else None, request.remote_addr))
    except Exception:
        pass

# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET","POST"])
def login():
    if q("SELECT COUNT(*) as n FROM admins", one=True)["n"] == 0:
        return redirect(url_for("first_setup"))
    if request.method == "POST":
        username = request.form.get("username","").strip()
        password = request.form.get("password","")
        admin = q("SELECT * FROM admins WHERE username=? AND is_active=1", (username,), one=True)
        if admin and bcrypt.checkpw(password.encode(), admin["password_hash"].encode()):
            token = secrets.token_hex(32)
            expires = (datetime.now()+timedelta(hours=12)).strftime("%Y-%m-%d %H:%M:%S")
            run("INSERT INTO admin_sessions(token,admin_id,role,expires_at) VALUES(?,?,?,?)",
                (token, admin["id"], admin["role"], expires))
            session["token"] = token
            session.permanent = True
            audit("login")
            return redirect(url_for("dashboard"))
        flash("Invalid credentials.", "error")
    return render_template("login.html")

@app.route("/first_setup", methods=["GET","POST"])
def first_setup():
    if q("SELECT COUNT(*) as n FROM admins", one=True)["n"] > 0:
        return redirect(url_for("login"))
    if request.method == "POST":
        username = request.form.get("username","").strip()
        password = request.form.get("password","")
        name = request.form.get("display_name","").strip()
        if not username or len(password) < 8:
            flash("Username required, password 8+ chars.", "error")
            return render_template("first_setup.html")
        pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        run("INSERT INTO admins(username,display_name,password_hash,role) VALUES(?,?,?,?)",
            (username, name or username, pw_hash, "superadmin"))
        flash("Account created! Log in.", "success")
        return redirect(url_for("login"))
    return render_template("first_setup.html")

@app.route("/logout")
def logout():
    token = session.get("token")
    if token:
        run("DELETE FROM admin_sessions WHERE token=?", (token,))
    session.clear()
    return redirect(url_for("login"))

# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def dashboard():
    communities = q("SELECT * FROM communities ORDER BY created_at DESC")
    stats = {}
    for c in communities:
        cid = c["id"]
        stats[cid] = {
            "listings": q("SELECT COUNT(*) as n FROM listings WHERE community_id=? AND status='active'", (cid,), one=True)["n"],
            "users": q("SELECT COUNT(*) as n FROM users WHERE community_id=?", (cid,), one=True)["n"],
            "pending": q("SELECT COUNT(*) as n FROM approval_queue aq JOIN listings l ON l.id=aq.listing_id WHERE l.community_id=? AND aq.status='pending'", (cid,), one=True)["n"],
            "groupbuys": q("SELECT COUNT(*) as n FROM group_buys WHERE community_id=? AND status='open'", (cid,), one=True)["n"],
            "invites_pending": q("SELECT COUNT(*) as n FROM invite_links WHERE community_id=? AND is_used=0 AND is_active=1", (cid,), one=True)["n"],
            "sponsor_inquiries": q("SELECT COUNT(*) as n FROM sponsorship_inquiries WHERE community_id=?", (cid,), one=True)["n"],
        }
    return render_template("dashboard.html", communities=communities, stats=stats, admin=g.admin)

# ── Communities ───────────────────────────────────────────────────────────────

@app.route("/communities")
@login_required
def communities():
    return render_template("communities.html",
                           communities=q("SELECT * FROM communities ORDER BY created_at DESC"),
                           admin=g.admin)

@app.route("/communities/add", methods=["GET","POST"])
@login_required
def community_add():
    if request.method == "POST":
        name = request.form.get("name","").strip()
        slug = request.form.get("slug","").strip().lower().replace(" ","_")
        if not name or not slug:
            flash("Name and slug required.", "error")
            return render_template("community_edit.html", community=None, admin=g.admin)
        run("INSERT INTO communities(name,slug,bot_token,description,welcome_msg,require_invite,max_invites_per_user) VALUES(?,?,?,?,?,?,?)",
            (name, slug, request.form.get("bot_token") or None,
             request.form.get("description"), request.form.get("welcome_msg","Welcome!"),
             int(request.form.get("require_invite",1)),
             int(request.form.get("max_invites_per_user",10))))
        audit("add_community", {"name": name})
        flash(f"Community '{name}' created.", "success")
        return redirect(url_for("communities"))
    return render_template("community_edit.html", community=None, admin=g.admin)

@app.route("/communities/<int:cid>/edit", methods=["GET","POST"])
@login_required
def community_edit(cid):
    community = q("SELECT * FROM communities WHERE id=?", (cid,), one=True)
    if not community:
        flash("Not found.", "error"); return redirect(url_for("communities"))
    if request.method == "POST":
        run("UPDATE communities SET name=?,bot_token=?,description=?,welcome_msg=?,is_active=?,require_invite=?,max_invites_per_user=? WHERE id=?",
            (request.form.get("name"), request.form.get("bot_token") or None,
             request.form.get("description"), request.form.get("welcome_msg"),
             int(request.form.get("is_active",1)),
             int(request.form.get("require_invite",1)),
             int(request.form.get("max_invites_per_user",10)), cid))
        audit("edit_community", {"id": cid})
        flash("Community updated.", "success")
        return redirect(url_for("communities"))
    return render_template("community_edit.html", community=dict(community), admin=g.admin)

# ── Invites ───────────────────────────────────────────────────────────────────

@app.route("/communities/<int:cid>/invites")
@login_required
def invites(cid):
    community = q("SELECT * FROM communities WHERE id=?", (cid,), one=True)
    invite_list = q("SELECT i.*,u1.display_name as created_by_name,u1.telegram_username as created_by_tg,"
                    "u2.display_name as used_by_name FROM invite_links i "
                    "LEFT JOIN users u1 ON u1.id=i.created_by "
                    "LEFT JOIN users u2 ON u2.id=i.used_by "
                    "WHERE i.community_id=? ORDER BY i.created_at DESC LIMIT 100", (cid,))
    return render_template("invites.html", invites=invite_list, community=dict(community), admin=g.admin)

@app.route("/communities/<int:cid>/invites/generate", methods=["POST"])
@login_required
def invite_generate(cid):
    code = secrets.token_urlsafe(12)
    run("INSERT INTO invite_links(community_id,code) VALUES(?,?)", (cid, code))
    audit("generate_invite", {"cid": cid, "code": code})
    flash(f"Invite code generated: {code}", "success")
    return redirect(url_for("invites", cid=cid))

@app.route("/invites/<int:inv_id>/revoke", methods=["POST"])
@login_required
def invite_revoke(inv_id):
    inv = q("SELECT * FROM invite_links WHERE id=?", (inv_id,), one=True)
    if inv:
        run("UPDATE invite_links SET is_active=0 WHERE id=?", (inv_id,))
        audit("revoke_invite", {"id": inv_id})
        flash("Invite revoked.", "warning")
        return redirect(url_for("invites", cid=inv["community_id"]))
    return redirect(url_for("dashboard"))

# ── Categories ────────────────────────────────────────────────────────────────

@app.route("/communities/<int:cid>/categories")
@login_required
def categories(cid):
    community = q("SELECT * FROM communities WHERE id=?", (cid,), one=True)
    cats = q("SELECT c.*,p.name as parent_name FROM categories c "
             "LEFT JOIN categories p ON p.id=c.parent_id "
             "WHERE c.community_id=? ORDER BY c.sort_order,c.name", (cid,))
    return render_template("categories.html", cats=cats, community=dict(community), admin=g.admin)

@app.route("/communities/<int:cid>/categories/add", methods=["GET","POST"])
@login_required
def category_add(cid):
    community = q("SELECT * FROM communities WHERE id=?", (cid,), one=True)
    parent_cats = q("SELECT id,name,icon FROM categories WHERE community_id=? AND is_active=1 ORDER BY name", (cid,))
    if request.method == "POST":
        schema_raw = request.form.get("schema_fields","[]").strip()
        try:
            json.loads(schema_raw)
        except Exception:
            flash("Invalid schema JSON.", "error")
            return render_template("category_edit.html", cat=None, community=dict(community),
                                   parent_cats=parent_cats, admin=g.admin)
        run("INSERT INTO categories(community_id,parent_id,name,slug,command,icon,description,"
            "requires_approval,listing_days,sort_order,start_date,end_date,schema_fields,"
            "max_listings_per_user_daily,max_listings_per_user_weekly,max_listings_per_user_monthly,"
            "max_listings_per_user_total) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, request.form.get("parent_id") or None,
             request.form.get("name","").strip(),
             request.form.get("slug","").strip().lower().replace(" ","_"),
             request.form.get("command","").strip().lower() or None,
             request.form.get("icon","📌"),
             request.form.get("description",""),
             int(request.form.get("requires_approval",0)),
             int(request.form.get("listing_days",60)),
             int(request.form.get("sort_order",0)),
             request.form.get("start_date") or None,
             request.form.get("end_date") or None,
             schema_raw,
             int(request.form.get("max_daily",0) or 0),
             int(request.form.get("max_weekly",0) or 0),
             int(request.form.get("max_monthly",0) or 0),
             int(request.form.get("max_total",0) or 0)))
        audit("add_category", {"cid": cid, "name": request.form.get("name")})
        flash("Category added.", "success")
        return redirect(url_for("categories", cid=cid))
    return render_template("category_edit.html", cat=None, community=dict(community),
                           parent_cats=parent_cats, admin=g.admin)

@app.route("/categories/<int:cat_id>/edit", methods=["GET","POST"])
@login_required
def category_edit(cat_id):
    cat = q("SELECT * FROM categories WHERE id=?", (cat_id,), one=True)
    if not cat:
        flash("Not found.", "error"); return redirect(url_for("dashboard"))
    community = q("SELECT * FROM communities WHERE id=?", (cat["community_id"],), one=True)
    parent_cats = q("SELECT id,name,icon FROM categories WHERE community_id=? AND id!=? ORDER BY name",
                    (cat["community_id"], cat_id))
    if request.method == "POST":
        schema_raw = request.form.get("schema_fields","[]").strip()
        try:
            json.loads(schema_raw)
        except Exception:
            flash("Invalid schema JSON.", "error")
            return render_template("category_edit.html", cat=dict(cat), community=dict(community),
                                   parent_cats=parent_cats, admin=g.admin)
        run("UPDATE categories SET name=?,slug=?,parent_id=?,command=?,icon=?,description=?,"
            "requires_approval=?,listing_days=?,sort_order=?,start_date=?,end_date=?,schema_fields=?,"
            "is_active=?,max_listings_per_user_daily=?,max_listings_per_user_weekly=?,"
            "max_listings_per_user_monthly=?,max_listings_per_user_total=? WHERE id=?",
            (request.form.get("name"), request.form.get("slug"),
             request.form.get("parent_id") or None, request.form.get("command") or None,
             request.form.get("icon","📌"), request.form.get("description",""),
             int(request.form.get("requires_approval",0)), int(request.form.get("listing_days",60)),
             int(request.form.get("sort_order",0)), request.form.get("start_date") or None,
             request.form.get("end_date") or None, schema_raw,
             int(request.form.get("is_active",1)),
             int(request.form.get("max_daily",0) or 0),
             int(request.form.get("max_weekly",0) or 0),
             int(request.form.get("max_monthly",0) or 0),
             int(request.form.get("max_total",0) or 0), cat_id))
        audit("edit_category", {"id": cat_id})
        flash("Category updated.", "success")
        return redirect(url_for("categories", cid=cat["community_id"]))
    return render_template("category_edit.html", cat=dict(cat), community=dict(community),
                           parent_cats=parent_cats, admin=g.admin)

@app.route("/categories/<int:cat_id>/delete", methods=["POST"])
@login_required
def category_delete(cat_id):
    cat = q("SELECT * FROM categories WHERE id=?", (cat_id,), one=True)
    if cat:
        run("DELETE FROM categories WHERE id=?", (cat_id,))
        audit("delete_category", {"id": cat_id})
        flash("Deleted.", "warning")
        return redirect(url_for("categories", cid=cat["community_id"]))
    return redirect(url_for("dashboard"))

@app.route("/communities/<int:cid>/categories/import_json", methods=["POST"])
@login_required
def category_import_json(cid):
    """Import one or multiple categories from JSON."""
    raw = request.form.get("json_data","").strip()
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        imported = 0
        for cat in data:
            slug = cat.get("slug","").lower().replace(" ","_")
            if not slug or not cat.get("name"):
                continue
            schema = json.dumps(cat.get("schema_fields", cat.get("fields", [])))
            try:
                json.loads(schema)
            except Exception:
                schema = "[]"
            run("INSERT OR REPLACE INTO categories(community_id,name,slug,icon,description,command,"
                "requires_approval,listing_days,sort_order,schema_fields,"
                "max_listings_per_user_daily,max_listings_per_user_weekly,"
                "max_listings_per_user_monthly,max_listings_per_user_total) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, cat.get("name"), slug, cat.get("icon","📌"),
                 cat.get("description",""), cat.get("command") or None,
                 int(cat.get("requires_approval",0)), int(cat.get("listing_days",60)),
                 int(cat.get("sort_order",0)), schema,
                 int(cat.get("max_daily",0) or 0), int(cat.get("max_weekly",0) or 0),
                 int(cat.get("max_monthly",0) or 0), int(cat.get("max_total",0) or 0)))
            imported += 1
        audit("import_categories_json", {"cid": cid, "count": imported})
        flash(f"{imported} category/categories imported.", "success")
    except Exception as e:
        flash(f"JSON error: {e}", "error")
    return redirect(url_for("categories", cid=cid))

# ── Listings ──────────────────────────────────────────────────────────────────

@app.route("/communities/<int:cid>/listings")
@login_required
def listings(cid):
    community = q("SELECT * FROM communities WHERE id=?", (cid,), one=True)
    status = request.args.get("status","active")
    cat_id = request.args.get("cat_id","")
    page = int(request.args.get("page",1))
    per = 30
    base = "FROM listings l JOIN categories c ON c.id=l.category_id JOIN users u ON u.id=l.user_id WHERE l.community_id=?"
    params = [cid]
    if status:
        base += " AND l.status=?"; params.append(status)
    if cat_id:
        base += " AND l.category_id=?"; params.append(int(cat_id))
    total = q(f"SELECT COUNT(*) as n {base}", params, one=True)["n"]
    rows = q(f"SELECT l.id,l.title,l.status,l.created_at,l.view_count,l.expires_at,c.name as cat_name,u.display_name,u.telegram_id {base} ORDER BY l.created_at DESC LIMIT {per} OFFSET {(page-1)*per}", params)
    cats = q("SELECT id,name FROM categories WHERE community_id=? ORDER BY name", (cid,))
    return render_template("listings.html", listings=rows, total=total, page=page,
                           per=per, community=dict(community), cats=cats,
                           status=status, cat_id=cat_id, admin=g.admin)

@app.route("/listings/<int:lid>/action", methods=["POST"])
@login_required
def listing_action(lid):
    action = request.form.get("action")
    listing = q("SELECT * FROM listings WHERE id=?", (lid,), one=True)
    if not listing:
        flash("Not found.", "error"); return redirect(url_for("dashboard"))
    cid = listing["community_id"]
    if action == "archive":
        run("UPDATE listings SET status='archived' WHERE id=?", (lid,)); flash("Archived.", "warning")
    elif action == "activate":
        run("UPDATE listings SET status='active' WHERE id=?", (lid,)); flash("Activated.", "success")
    elif action == "approve":
        run("UPDATE listings SET status='active' WHERE id=?", (lid,))
        run("UPDATE approval_queue SET status='approved',reviewed_at=datetime('now') WHERE listing_id=?", (lid,))
        flash("Approved.", "success")
    elif action == "reject":
        run("UPDATE listings SET status='archived' WHERE id=?", (lid,))
        run("UPDATE approval_queue SET status='rejected',reviewed_at=datetime('now') WHERE listing_id=?", (lid,))
        flash("Rejected.", "warning")
    audit(f"listing_{action}", {"id": lid})
    return redirect(request.referrer or url_for("listings", cid=cid))

# ── Users ─────────────────────────────────────────────────────────────────────

@app.route("/communities/<int:cid>/users")
@login_required
def users(cid):
    community = q("SELECT * FROM communities WHERE id=?", (cid,), one=True)
    rows = q("SELECT u.*,u2.display_name as invited_by_name FROM users u "
             "LEFT JOIN users u2 ON u2.id=u.invited_by "
             "WHERE u.community_id=? ORDER BY u.joined_at DESC", (cid,))
    return render_template("users.html", users=rows, community=dict(community), admin=g.admin)

@app.route("/users/<int:uid>/action", methods=["POST"])
@login_required
def user_action(uid):
    action = request.form.get("action")
    user = q("SELECT * FROM users WHERE id=?", (uid,), one=True)
    if not user:
        return redirect(url_for("dashboard"))
    if action == "ban":
        reason = request.form.get("reason","Violation of community rules")
        run("UPDATE users SET is_banned=1,ban_reason=? WHERE id=?", (reason, uid))
        # Revoke their unused invites
        run("UPDATE invite_links SET is_active=0 WHERE created_by=? AND is_used=0", (uid,))
        flash("User banned and invites revoked.", "warning")
    elif action == "unban":
        run("UPDATE users SET is_banned=0,ban_reason=NULL WHERE id=?", (uid,))
        flash("User unbanned.", "success")
    elif action == "verify":
        run("UPDATE users SET is_verified=1 WHERE id=?", (uid,))
        flash("User verified.", "success")
    audit(f"user_{action}", {"user_id": uid})
    return redirect(request.referrer or url_for("dashboard"))

# ── Sponsorships ──────────────────────────────────────────────────────────────

@app.route("/communities/<int:cid>/sponsorships")
@login_required
def sponsorships(cid):
    community = q("SELECT * FROM communities WHERE id=?", (cid,), one=True)
    sponsors = q("SELECT * FROM sponsorships WHERE community_id=? ORDER BY created_at DESC", (cid,))
    inquiries = q("SELECT * FROM sponsorship_inquiries WHERE community_id=? ORDER BY created_at DESC", (cid,))
    return render_template("sponsorships.html", sponsors=sponsors, inquiries=inquiries,
                           community=dict(community), admin=g.admin)

@app.route("/communities/<int:cid>/sponsorships/add", methods=["POST"])
@login_required
def sponsorship_add(cid):
    run("INSERT INTO sponsorships(community_id,sponsor_name,banner_text,description,link,image_url,shows_per_day,start_date,end_date,is_active) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (cid, request.form.get("sponsor_name","").strip(),
         request.form.get("banner_text","").strip(),
         request.form.get("description","").strip(),
         request.form.get("link","").strip() or None,
         request.form.get("image_url","").strip() or None,
         int(request.form.get("shows_per_day",3)),
         request.form.get("start_date"), request.form.get("end_date"), 1))
    audit("add_sponsorship", {"cid": cid})
    flash("Sponsorship added.", "success")
    return redirect(url_for("sponsorships", cid=cid))

@app.route("/sponsorships/<int:sid>/toggle", methods=["POST"])
@login_required
def sponsorship_toggle(sid):
    s = q("SELECT * FROM sponsorships WHERE id=?", (sid,), one=True)
    if s:
        run("UPDATE sponsorships SET is_active=? WHERE id=?", (0 if s["is_active"] else 1, sid))
        return redirect(url_for("sponsorships", cid=s["community_id"]))
    return redirect(url_for("dashboard"))

@app.route("/sponsorships/<int:sid>/delete", methods=["POST"])
@login_required
def sponsorship_delete(sid):
    s = q("SELECT * FROM sponsorships WHERE id=?", (sid,), one=True)
    if s:
        cid = s["community_id"]
        run("DELETE FROM sponsorships WHERE id=?", (sid,))
        return redirect(url_for("sponsorships", cid=cid))
    return redirect(url_for("dashboard"))

# ── Group Buying ──────────────────────────────────────────────────────────────

@app.route("/communities/<int:cid>/groupbuys")
@login_required
def groupbuys(cid):
    community = q("SELECT * FROM communities WHERE id=?", (cid,), one=True)
    gbs = q("SELECT gb.*,c.name as cat_name FROM group_buys gb LEFT JOIN categories c ON c.id=gb.category_id WHERE gb.community_id=? ORDER BY gb.created_at DESC", (cid,))
    for gb in gbs:
        e = q("SELECT COUNT(*) as cnt,SUM(tickets) as tot FROM group_buy_entries WHERE group_buy_id=?", (gb["id"],), one=True)
        gb["entry_count"] = e["cnt"] or 0
        gb["total_amount"] = (e.get("tot") or 0) * (gb.get("ticket_price") or 0)
    cats = q("SELECT id,name FROM categories WHERE community_id=? AND is_active=1 ORDER BY name", (cid,))
    return render_template("groupbuys.html", groupbuys=gbs, community=dict(community), cats=cats, admin=g.admin)

@app.route("/communities/<int:cid>/groupbuys/add", methods=["POST"])
@login_required
def groupbuy_add(cid):
    run("INSERT INTO group_buys(community_id,category_id,title,description,item_url,ticket_price,"
        "threshold_amount,advance_percent,max_tickets_per_user,reward_percent,expires_at,telegram_group_link,status) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'open')",
        (cid, request.form.get("category_id") or None,
         request.form.get("title","").strip(),
         request.form.get("description","").strip(),
         request.form.get("item_url","").strip() or None,
         float(request.form.get("ticket_price") or 0),
         float(request.form.get("threshold_amount") or 0),
         float(request.form.get("advance_percent") or 20),
         int(request.form.get("max_tickets_per_user") or 10),
         float(request.form.get("reward_percent") or 0.5),
         request.form.get("expires_at") or None,
         request.form.get("telegram_group_link","").strip() or None))
    audit("add_groupbuy", {"cid": cid})
    flash("Group buy created.", "success")
    return redirect(url_for("groupbuys", cid=cid))

@app.route("/groupbuys/<int:gb_id>")
@login_required
def groupbuy_detail(gb_id):
    gb = q("SELECT * FROM group_buys WHERE id=?", (gb_id,), one=True)
    if not gb:
        flash("Not found.", "error"); return redirect(url_for("dashboard"))
    entries = q("SELECT gbe.*,u.display_name,u.telegram_username,u.flat_number,u.telegram_id "
                "FROM group_buy_entries gbe JOIN users u ON u.id=gbe.user_id "
                "WHERE gbe.group_buy_id=? ORDER BY gbe.votes_received DESC,gbe.created_at", (gb_id,))
    community = q("SELECT * FROM communities WHERE id=?", (gb["community_id"],), one=True)
    total_tickets = sum(e.get("tickets") or 0 for e in entries)
    total_amount = total_tickets * (gb.get("ticket_price") or 0)
    total_advance = sum((e.get("tickets") or 0)*(gb.get("ticket_price") or 0)*(gb.get("advance_percent") or 20)/100 for e in entries)
    paid_advance = sum((e.get("tickets") or 0)*(gb.get("ticket_price") or 0)*(gb.get("advance_percent") or 20)/100 for e in entries if e.get("advance_paid"))
    return render_template("groupbuy_detail.html", gb=dict(gb), entries=entries,
                           community=dict(community), total_tickets=total_tickets,
                           total_amount=total_amount, total_advance=total_advance,
                           paid_advance=paid_advance, admin=g.admin)

@app.route("/groupbuys/<int:gb_id>/update", methods=["POST"])
@login_required
def groupbuy_update(gb_id):
    gb = q("SELECT * FROM group_buys WHERE id=?", (gb_id,), one=True)
    if not gb:
        return redirect(url_for("dashboard"))
    action = request.form.get("action")
    if action == "close":
        run("UPDATE group_buys SET status='closed' WHERE id=?", (gb_id,)); flash("Closed.", "warning")
    elif action == "complete":
        run("UPDATE group_buys SET status='completed' WHERE id=?", (gb_id,)); flash("Completed.", "success")
    elif action == "set_link":
        run("UPDATE group_buys SET telegram_group_link=? WHERE id=?", (request.form.get("link",""), gb_id)); flash("Link updated.", "success")
    elif action == "mark_paid":
        run("UPDATE group_buy_entries SET advance_paid=1 WHERE id=?", (request.form.get("entry_id"),)); flash("Marked paid.", "success")
    elif action == "forfeit":
        run("UPDATE group_buy_entries SET forfeited=1 WHERE id=?", (request.form.get("entry_id"),)); flash("Forfeited.", "warning")
    elif action == "set_winner":
        entry = q("SELECT user_id FROM group_buy_entries WHERE id=?", (request.form.get("entry_id"),), one=True)
        if entry:
            run("UPDATE group_buys SET winner_user_id=?,status='winner_selected' WHERE id=?", (entry["user_id"], gb_id)); flash("Winner set!", "success")
    audit(f"groupbuy_{action}", {"gb_id": gb_id})
    return redirect(url_for("groupbuy_detail", gb_id=gb_id))

# ── Approvals ─────────────────────────────────────────────────────────────────

@app.route("/communities/<int:cid>/approvals")
@login_required
def approvals(cid):
    community = q("SELECT * FROM communities WHERE id=?", (cid,), one=True)
    rows = q("SELECT aq.*,l.title,l.data,c.name as cat_name,c.schema_fields,"
             "u.display_name,u.telegram_username,u.telegram_id "
             "FROM approval_queue aq JOIN listings l ON l.id=aq.listing_id "
             "JOIN categories c ON c.id=l.category_id JOIN users u ON u.id=aq.submitted_by "
             "WHERE l.community_id=? AND aq.status='pending' ORDER BY aq.created_at", (cid,))
    for r in rows:
        r["data_parsed"] = json.loads(r.get("data") or "{}")
        r["fields"] = json.loads(r.get("schema_fields") or "[]")
    return render_template("approvals.html", items=rows, community=dict(community), admin=g.admin)

# ── Admins ────────────────────────────────────────────────────────────────────

@app.route("/admins")
@login_required
def admin_list():
    return render_template("admin_list.html",
                           admins=q("SELECT id,username,display_name,role,is_active,created_at FROM admins ORDER BY created_at"),
                           admin=g.admin)

@app.route("/admins/add", methods=["POST"])
@login_required
def admin_add():
    username = request.form.get("username","").strip()
    password = request.form.get("password","")
    if not username or len(password) < 8:
        flash("Username required, password 8+ chars.", "error")
        return redirect(url_for("admin_list"))
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    try:
        run("INSERT INTO admins(username,display_name,password_hash,role) VALUES(?,?,?,?)",
            (username, request.form.get("display_name","").strip() or username,
             pw_hash, request.form.get("role","admin")))
        flash(f"Admin '{username}' added.", "success")
    except Exception:
        flash("Username already exists.", "error")
    return redirect(url_for("admin_list"))

@app.route("/admins/<int:aid>/toggle", methods=["POST"])
@login_required
def admin_toggle(aid):
    a = q("SELECT * FROM admins WHERE id=?", (aid,), one=True)
    if a:
        run("UPDATE admins SET is_active=? WHERE id=?", (0 if a["is_active"] else 1, aid))
    return redirect(url_for("admin_list"))

# ── Audit ─────────────────────────────────────────────────────────────────────

@app.route("/audit")
@login_required
def audit_log():
    rows = q("SELECT al.*,a.username FROM audit_log al LEFT JOIN admins a ON a.id=al.admin_id ORDER BY al.at DESC LIMIT 200")
    return render_template("audit.html", rows=rows, admin=g.admin)

# ── Error handler ─────────────────────────────────────────────────────────────

@app.errorhandler(500)
def error_500(e):
    import traceback
    err = traceback.format_exc()
    try:
        with open(os.environ.get("LOG_PATH","/app/logs/dashboard.log"),"a") as f:
            f.write(f"\n{'='*60}\n{datetime.now()}\n{err}\n")
    except Exception:
        pass
    return f"<pre style='padding:20px;background:#111;color:#f87;'>{err[:3000]}</pre>", 500

if __name__ == "__main__":
    init_db()
    os.makedirs(os.environ.get("LOG_DIR","/app/logs"), exist_ok=True)
    print("\n🌐 Dashboard: http://0.0.0.0:5556\n")
    app.run(host="0.0.0.0", port=5556, debug=False)
