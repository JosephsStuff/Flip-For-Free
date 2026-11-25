"""Flip for Free - local web app.

    python app.py            # https://localhost:8765  (self-signed certificate)
    python app.py --demo     # try it with fake data, no eBay account needed
    python app.py --no-https # plain http (only if your eBay RuName accepts http)
"""
import argparse
import csv
import io
import os
import re
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template, request,
                   session, url_for)

import automation
import db
import markets
import updater
import profit
import sync
from common import (PERIODS, closed_guard, db_ready, login_required, parse_int, parse_money, period_range,
                    sales_where)
from db import Settings, connect, write_lock
from ebay_api import EbayClient, EbayError, new_state
from markets import money as fmt_money

APP_DIR = Path(__file__).resolve().parent
app = Flask(__name__, template_folder=str(APP_DIR / "templates"),
            static_folder=str(APP_DIR / "static"))
app.config["DEMO"] = False
app.config["PORT"] = 8765
app.config["SCHEME"] = "https"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
@app.template_filter("gbp")
@app.template_filter("money")
def gbp(v, blank="—"):
    """Money in your currency (older templates still call the filter gbp)."""
    return fmt_money(v, blank)


@app.template_filter("pct")
def pct(v):
    return "—" if v is None else f"{v:,.0f}%"


@app.template_filter("d")
def fmt_date(v):
    if not v:
        return ""
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).strftime("%d %b %Y")
    except ValueError:
        return str(v)[:10]


@app.template_filter("tidy_event")
def tidy_event(text):
    """Timeline text without eBay's internal line numbers ('Sold 1 for £135.46 (line 13106464319)')."""
    return re.sub(r"\s*\(line [\w-]+\)", "", text or "")


@app.template_filter("ago")
def ago(v):
    if not v:
        return "never"
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return v
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    if secs < 90:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if secs < 86400:
        return f"{int(secs // 3600)} h ago"
    return f"{int(secs // 86400)} days ago"


@app.before_request
def same_origin_check():
    """Basic CSRF protection: POSTs must come from this app's own pages."""
    if request.method == "POST":
        origin = request.headers.get("Origin") or request.headers.get("Referer")
        allowed = {request.host}
        if app.config.get("PUBLIC_URL"):
            allowed.add(urlparse(app.config["PUBLIC_URL"]).netloc)
        if origin and urlparse(origin).netloc not in allowed:
            abort(403)


@app.context_processor
def inject():
    from common import period_label, saved_ranges
    ctx = {"demo": app.config["DEMO"], "sync_status": sync.STATUS, "db_path": db.get_db_path(),
           "user_name": session.get("user_name"), "device_warning": None, "last_sync": None, "unread": 0,
           "saved_ranges": [], "period_label": period_label, "beta": updater.installed_channel() == "beta",
           "cur": markets.symbol(), "market": markets.site(), "tax_profile": markets.tax(), "unit": markets.unit_word(),
           "ebay_item_url": markets.listing_url,
           "fee_defaults": markets.fee_defaults(), "all_sites": markets.SITES}
    try:
        ctx["saved_ranges"] = saved_ranges()
    except Exception:
        pass
    if db_ready():
        try:
            db.ensure_schema()
            c = connect()
            try:
                ctx["unread"] = c.execute("SELECT COUNT(*) FROM notifications WHERE read=0").fetchone()[0]
            finally:
                c.close()
            s = Settings().get_many(["last_device", "last_device_seen", "last_sync_orders"])
            ctx["last_sync"] = s.get("last_sync_orders")
            other = s.get("last_device")
            seen = s.get("last_device_seen")
            if other and other != db.device_name() and seen:
                secs = (datetime.now(timezone.utc) -
                        datetime.fromisoformat(seen.replace("Z", "+00:00"))).total_seconds()
                if secs < 600:
                    ctx["device_warning"] = other
        except Exception:
            pass
    return ctx


_last_heartbeat = {"t": 0}


@app.after_request
def heartbeat(resp):
    # Remember which device last used the DB (to warn about simultaneous use).
    if db_ready() and time.time() - _last_heartbeat["t"] > 240 and request.endpoint != "static":
        _last_heartbeat["t"] = time.time()
        try:
            Settings().set_many({"last_device": db.device_name(), "last_device_seen": db.now_iso()})
        except Exception:
            pass
    return resp


# --------------------------------------------------------------------------- #
# Setup: database location + eBay app keys
# --------------------------------------------------------------------------- #
@app.route("/setup")
def setup():
    cfg = db.load_config()
    ebay = {}
    if db_ready():
        ebay = Settings().get_many(["ebay_env", "client_id", "dev_id", "ru_name", "marketplace_id"])
    callback = (app.config["PUBLIC_URL"] + "/auth/callback" if app.config.get("PUBLIC_URL")
                else f"{app.config['SCHEME']}://localhost:{app.config['PORT']}/auth/callback")
    current_folder = str(Path(cfg["db_path"]).parent) if cfg.get("db_path") else ""
    return render_template("setup.html", cfg=cfg, ready=db_ready(), ebay=ebay,
                           current_folder=current_folder,
                           suggestions=db.suggested_cloud_folders(), callback=callback,
                           default_name=db.DB_FILENAME)


@app.route("/setup/db", methods=["POST"])
def setup_db():
    if db_ready() and session.get("user_id") is None and not app.config["DEMO"] \
            and EbayClient().configured and Settings().get("owner_user_id"):
        # Changing DB while one is configured requires being signed in.
        flash("Sign in first to change the database location.", "error")
        return redirect(url_for("login"))
    mode = request.form.get("mode", "folder")
    raw = (request.form.get("path") or "").strip().strip('"')
    if not raw:
        flash("Choose a folder or file.", "error")
        return redirect(url_for("setup"))
    p = Path(os.path.expanduser(raw))
    if mode == "folder" or p.is_dir():
        if not p.exists():
            flash(f"Folder not found: {p}", "error")
            return redirect(url_for("setup"))
        p = p / db.DB_FILENAME
    if p.exists():
        if not db.is_tracker_db(str(p)):
            flash(f"{p} exists but isn't an Flip for Free database.", "error")
            return redirect(url_for("setup"))
        db.init_db(str(p))  # applies any new tables
        flash(f"Opened existing database: {p}", "ok")
    else:
        db.init_db(str(p))
        flash(f"Created new database: {p}", "ok")
    db.set_db_path(str(p))
    session.clear()
    EbayClient.forget_tokens()
    return redirect(url_for("setup") if not EbayClient().configured else url_for("login"))


@app.route("/setup/ebay", methods=["POST"])
def setup_ebay():
    if not db_ready():
        return redirect(url_for("setup"))
    st = Settings()
    if st.get("owner_user_id") and not session.get("user_id") and not app.config["DEMO"]:
        flash("Sign in first to change eBay app keys.", "error")
        return redirect(url_for("login"))
    vals = {
        "ebay_env": request.form.get("ebay_env", "production"),
        "client_id": request.form.get("client_id", "").strip(),
        "ru_name": request.form.get("ru_name", "").strip(),
        "dev_id": request.form.get("dev_id", "").strip(),
        "marketplace_id": request.form.get("marketplace_id", "EBAY_GB").strip() or "EBAY_GB",
    }
    secret = request.form.get("client_secret", "").strip()
    if secret:
        vals["client_secret"] = secret
    problem = _key_problem(vals["client_id"], vals["ru_name"], vals["ebay_env"])
    if problem:
        flash(problem, "error")
        return redirect(url_for("setup"))
    if vals["ru_name"].lower().startswith("http"):
        flash("RuName is the short name in the 'RuName (eBay Redirect URL name)' column, not the URL.", "error")
        return redirect(url_for("setup"))
    if secret and not secret.upper().startswith(("PRD-", "SBX-")):
        flash("Cert ID (Client Secret) normally starts with PRD- (or SBX- for sandbox). Check you copied the right box.", "error")
    if not vals["client_id"] or not vals["ru_name"] or not (secret or st.get("client_secret")):
        flash("Client ID, Client Secret and RuName are all needed.", "error")
        return redirect(url_for("setup"))
    old_env = st.get("ebay_env")
    st.set_many(vals)
    if old_env and old_env != vals["ebay_env"]:
        st.set_many({"refresh_token": None, "signing_jwe": None, "signing_private_key": None})
    EbayClient.forget_tokens()
    flash("eBay keys saved.", "ok")
    return redirect(url_for("login"))


@app.route("/api/browse")
def api_browse():
    raw = request.args.get("path") or str(Path.home())
    p = Path(os.path.expanduser(raw))
    try:
        if not p.is_dir():
            p = p.parent
        dirs = sorted([c.name for c in p.iterdir() if c.is_dir() and not c.name.startswith(".")],
                      key=str.lower)[:400]
        dbs = [c.name for c in p.iterdir() if c.is_file() and c.suffix == ".db"]
    except (PermissionError, OSError) as e:
        return jsonify({"path": str(p), "parent": str(p.parent), "dirs": [], "dbs": [],
                        "error": str(e)})
    return jsonify({"path": str(p), "parent": str(p.parent), "dirs": dirs, "dbs": dbs})


# --------------------------------------------------------------------------- #
# eBay SSO
# --------------------------------------------------------------------------- #
@app.route("/login")
def login():
    if not db_ready():
        return redirect(url_for("setup"))
    if app.config["DEMO"]:
        return redirect(url_for("dashboard"))
    client = EbayClient()
    if not client.configured:
        return redirect(url_for("setup"))
    owner = Settings().get("owner_username")
    return render_template("login.html", owner=owner, env=client.env_name)


@app.route("/auth/start")
def auth_start():
    client = EbayClient()
    if not client.configured:
        return redirect(url_for("setup"))
    problem = _key_problem(client.client_id, client.ru_name, client.env_name)
    if problem:
        flash(problem + " Fix it below, then sign in again.", "error")
        return redirect(url_for("setup"))
    # port prefix lets the relay page send you back to the right local address
    state = f"{app.config['PORT']}.{new_state()}"
    session["oauth_state"] = state
    session["next"] = request.args.get("next") or url_for("dashboard")
    return redirect(client.authorize_url(state))


UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
APPID_RE = re.compile(r"-(PRD|SBX)-[0-9a-f]{8,}-[0-9a-f]{8}$", re.I)


def _key_problem(client_id, ru_name, env):
    if UUID_RE.match(client_id or ""):
        return "The App ID (Client ID) box contains your Dev ID."
    tag = "SBX" if env == "sandbox" else "PRD"
    if f"-{tag}-" not in (client_id or "").upper():
        return f"App ID (Client ID) should contain '-{tag}-' for {env}."
    if (ru_name or "") == client_id or APPID_RE.search(ru_name or ""):
        return ("The RuName box contains your App ID. Create a Redirect URL on eBay's User Tokens page "
                "and paste its RuName.")
    if (ru_name or "").lower().startswith("http"):
        return "RuName is the short name from the RuName column, not a web address."
    return None


@app.route("/auth/callback")
def auth_callback():
    return _finish_auth(request.args.to_dict())


@app.route("/auth/paste", methods=["POST"])
def auth_paste():
    """Fallback when eBay won't redirect to localhost: user pastes the URL eBay landed on."""
    raw = (request.form.get("url") or "").strip()
    params = {}
    if "?" in raw or "code=" in raw:
        params = {k: v[0] for k, v in parse_qs(raw.split("?", 1)[-1]).items()}
    elif raw:
        params = {"code": unquote(raw)}
    if not params.get("code") and not params.get("error"):
        flash("That link doesn't contain an eBay sign-in code. Copy the whole address from the page "
              "eBay showed after you clicked 'Agree'.", "error")
        return redirect(url_for("login"))
    return _finish_auth(params, pasted=True)


def _finish_auth(args: dict, pasted: bool = False):
    if args.get("error") or args.get("isAuthSuccessful") == "false":
        flash(f"eBay sign-in didn't complete: {args.get('error_description') or args.get('error') or 'declined'}",
              "error")
        return redirect(url_for("login"))
    expected = session.get("oauth_state")
    got = args.get("state")
    if not expected or (got and got != expected) or (not got and not pasted):
        flash("Sign-in expired, please click Sign in with eBay again.", "error")
        return redirect(url_for("login"))
    session.pop("oauth_state", None)
    code = args.get("code")
    client = EbayClient()
    st = Settings()
    try:
        prev_refresh = st.get("refresh_token")
        prev_expiry = st.get("refresh_token_expiry")
        client.exchange_code(code)
        user = client.get_user()
    except EbayError as e:
        msg = str(e)
        if "invalid_grant" in msg:
            msg = ("eBay rejected the sign-in code. It's only valid for 5 minutes and works once, so click "
                   "Sign in with eBay again. If it keeps happening, check the RuName matches this keyset.")
        elif "invalid_client" in msg:
            msg = ("eBay didn't accept your App ID (Client ID) or Cert ID (Client Secret). Re-copy them from the "
                   "Production keyset (and check Environment is set to Production).")
        flash(msg, "error")
        return redirect(url_for("login"))
    uid = user.get("userId") or user.get("username")
    uname = user.get("username") or uid
    owner = st.get("owner_user_id")
    if owner and owner != uid:
        # restore the owner's token - a different account must not take over
        st.set_many({"refresh_token": prev_refresh, "refresh_token_expiry": prev_expiry})
        EbayClient.forget_tokens()
        flash(f"This database belongs to eBay account '{st.get('owner_username')}'. "
              f"You signed in as '{uname}'.", "error")
        return redirect(url_for("login"))
    if not owner:
        st.set_many({"owner_user_id": uid, "owner_username": uname})
    nxt = session.get("next") or url_for("dashboard")
    session.clear()
    session.permanent = True
    session.update(user_id=uid, user_name=uname, db_path=db.get_db_path())
    if not st.get("last_sync_orders"):
        sync.start_background()
        flash("Signed in. Your first sync has started - this can take a few minutes.", "ok")
    return redirect(nxt if nxt.startswith("/") else url_for("dashboard"))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #
@app.route("/")
@login_required
def dashboard():
    key = request.args.get("period", "30d")
    start, end = period_range(key, request.args.get("start"), request.args.get("end"))
    conn = connect()
    try:
        where, params = sales_where(start, end)
        sales = profit.compute_sales(conn, where, tuple(params))
        tot = profit.summarise(sales)
        include_acct = Settings().get("include_account_fees", "1") == "1"
        acct = profit.account_charges(conn, start, end)
        bq = "SELECT * FROM bulk_buys WHERE 1=1"
        bp = []
        if start:
            bq += " AND purchased_at >= ?"
            bp.append(start)
        if end:
            bq += " AND purchased_at < ?"
            bp.append(end)
        bulks_in_period = [dict(r) for r in conn.execute(bq, bp)]
        bulk_cost = sum((b["total_cost"] or 0) + (b["extra_costs"] or 0) for b in bulks_in_period)
        acct_total = acct["total"] if include_acct else 0
        basis = Settings().get("profit_basis", "bought")
        if basis == "sold":
            # cost of goods sold: every sale carries its own (or its bulk share of) cost
            true_profit = tot["money_made"] - tot["item_costs"] - tot["bulk_alloc"] - acct_total
        else:
            true_profit = tot["money_made"] - tot["item_costs"] - bulk_cost - acct_total
        # chart: last 12 months regardless of period
        cs, _ = period_range("all")
        chart_start = (date.today().replace(day=1) - timedelta(days=335)).replace(day=1).isoformat()
        cw, cp = sales_where(chart_start, None)
        chart_sales = profit.compute_sales(conn, cw, tuple(cp))
        bcm = {}
        for r in conn.execute("SELECT purchased_at, total_cost, extra_costs FROM bulk_buys "
                              "WHERE purchased_at >= ?", (chart_start,)):
            k = (r["purchased_at"] or "")[:7]
            bcm[k] = bcm.get(k, 0) + (r["total_cost"] or 0) + (r["extra_costs"] or 0)
        months = profit.monthly(chart_sales, bcm)
        stock = profit.stock_on_hand(conn)
        top = sorted([s for s in sales if s["profit"] is not None],
                     key=lambda s: -s["profit"])[:5]
        open_bulks = []
        for b in conn.execute("SELECT * FROM bulk_buys ORDER BY purchased_at DESC LIMIT 6"):
            b = dict(b)
            bs = profit.compute_sales(conn, "s.bulk_buy_id = ?", (b["id"],))
            open_bulks.append({**b, **profit.bulk_summary(b, bs)})
    finally:
        conn.close()
    return render_template(
        "dashboard.html", tot=tot, acct=acct, include_acct=include_acct, bulk_cost=bulk_cost,
        bulks_in_period=bulks_in_period, true_profit=true_profit, months=months, top=top,
        open_bulks=open_bulks, period=key, periods=PERIODS, basis=basis, stock=stock,
        start=request.args.get("start", ""), end=request.args.get("end", ""))


# --------------------------------------------------------------------------- #
# Sales
# --------------------------------------------------------------------------- #
@app.route("/sales")
@login_required
def sales_list():
    key = request.args.get("period", "all")
    start, end = period_range(key, request.args.get("start"), request.args.get("end"))
    q = (request.args.get("q") or "").strip()
    bulk = request.args.get("bulk", "")
    flt = request.args.get("filter", "")
    extra, extra_p = [], []
    if q:
        extra.append("(s.title LIKE ? OR s.order_id LIKE ? OR s.legacy_item_id LIKE ? OR s.sku LIKE ?)")
        extra_p += [f"%{q}%"] * 4
    if bulk == "none":
        extra.append("s.bulk_buy_id IS NULL")
    elif bulk:
        extra.append("s.bulk_buy_id = ?")
        extra_p.append(parse_int(bulk))
    if flt == "missing_cost":
        extra.append("s.bulk_buy_id IS NULL AND s.cost IS NULL")
    where, params = sales_where(start, end, " AND ".join(extra))
    conn = connect()
    try:
        sales = profit.compute_sales(conn, where, tuple(params + extra_p))
        bulks = [dict(r) for r in conn.execute("SELECT id, name FROM bulk_buys ORDER BY name")]
    finally:
        conn.close()
    tot = profit.summarise(sales)
    return render_template("sales.html", sales=sales, tot=tot, bulks=bulks, q=q, bulk=bulk,
                           filter=flt, period=key, periods=PERIODS,
                           start=request.args.get("start", ""), end=request.args.get("end", ""))


@app.route("/sales/<line_id>", methods=["GET", "POST"])
@login_required
def sale_detail(line_id):
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM sales WHERE line_item_id=?", (line_id,)).fetchone()
        if not row:
            abort(404)
        if request.method == "POST":
            guard = closed_guard([row["sold_at"]], "Changing this sale's costs")
            if guard:
                return guard
            bulk_id = parse_int(request.form.get("bulk_buy_id"))
            cost = parse_money(request.form.get("cost"))
            vals = (cost, parse_money(request.form.get("postage_cost")),
                    parse_money(request.form.get("packaging_cost")), parse_money(request.form.get("handling_cost")),
                    parse_money(request.form.get("other_cost")), bulk_id,
                    request.form.get("notes") or None, db.now_iso(), line_id)
            with write_lock():
                conn.execute("""UPDATE sales SET cost=?, postage_cost=?, packaging_cost=?, handling_cost=?,
                                other_cost=?, bulk_buy_id=?, notes=?, updated_at=? WHERE line_item_id=?""",
                             vals)
                if request.form.get("apply_listing") and row["legacy_item_id"]:
                    qty = row["quantity"] or 1
                    unit = None if cost is None else round(cost / qty, 4)
                    _apply_listing_rule(conn, row["legacy_item_id"], bulk_id, unit)
                conn.commit()
            flash("Saved.", "ok")
            return redirect(request.form.get("back") or url_for("sale_detail", line_id=line_id))
        sale = profit.compute_sales(conn, "s.line_item_id = ?", (line_id,))[0]
        txs = [dict(r) for r in conn.execute(
            "SELECT * FROM transactions WHERE order_id=? ORDER BY date", (row["order_id"],))]
        others = [dict(r) for r in conn.execute(
            "SELECT line_item_id, title FROM sales WHERE order_id=? AND line_item_id<>?",
            (row["order_id"], line_id))]
        bulks = [dict(r) for r in conn.execute("SELECT id, name FROM bulk_buys ORDER BY name")]
        rule = conn.execute("SELECT * FROM listing_rules WHERE legacy_item_id=?",
                            (row["legacy_item_id"],)).fetchone() if row["legacy_item_id"] else None
        same_listing = conn.execute("SELECT COUNT(*) FROM sales WHERE legacy_item_id=?",
                                    (row["legacy_item_id"],)).fetchone()[0] if row["legacy_item_id"] else 0
    finally:
        conn.close()
    return render_template("sale.html", s=sale, txs=txs, others=others, bulks=bulks, rule=rule,
                           same_listing=same_listing, tx_label=profit.tx_label, signed=profit.signed,
                           back=request.args.get("back", ""))


def _apply_listing_rule(conn, legacy, bulk_id, unit_cost):
    """Apply bulk buy / unit cost to every sale of a listing, now and in future syncs."""
    if bulk_id is None and unit_cost is None:
        conn.execute("DELETE FROM listing_rules WHERE legacy_item_id=?", (legacy,))
        conn.execute("UPDATE sales SET bulk_buy_id=NULL WHERE legacy_item_id=?", (legacy,))
        return
    conn.execute("""INSERT INTO listing_rules(legacy_item_id, bulk_buy_id, unit_cost) VALUES (?,?,?)
                    ON CONFLICT(legacy_item_id) DO UPDATE SET bulk_buy_id=excluded.bulk_buy_id,
                    unit_cost=excluded.unit_cost""", (legacy, bulk_id, unit_cost))
    conn.execute("""UPDATE sales SET bulk_buy_id=?,
                    cost=CASE WHEN ? IS NULL THEN cost ELSE ROUND(? * COALESCE(quantity,1), 2) END
                    WHERE legacy_item_id=?""", (bulk_id, unit_cost, unit_cost, legacy))


@app.route("/sales/assign", methods=["POST"])
@login_required
def sales_assign():
    ids = request.form.getlist("ids")
    action = request.form.get("action")
    back = request.form.get("back") or url_for("sales_list")
    if not ids:
        flash("Tick at least one sale first.", "error")
        return redirect(back)
    conn = connect()
    try:
        ph = ",".join("?" * len(ids))
        dates = [r[0] for r in conn.execute(f"SELECT sold_at FROM sales WHERE line_item_id IN ({ph})", ids)]
        guard = closed_guard(dates, f"Changing {len(ids)} sale(s)")
        if guard:
            return guard
        with write_lock():
            if action == "bulk":
                bulk_id = request.form.get("bulk_buy_id")
                if bulk_id == "new":
                    name = (request.form.get("new_bulk_name") or "").strip() or "New bulk buy"
                    cur = conn.execute("INSERT INTO bulk_buys(name, purchased_at, total_cost, created_at) "
                                       "VALUES (?,?,0,?)", (name, date.today().isoformat(), db.now_iso()))
                    bulk_id = cur.lastrowid
                else:
                    bulk_id = parse_int(bulk_id)
                conn.execute(f"UPDATE sales SET bulk_buy_id=? WHERE line_item_id IN ({ph})",
                             [bulk_id] + ids)
                msg = f"{len(ids)} sale(s) {'assigned to bulk buy' if bulk_id else 'removed from bulk buy'}."
            elif action == "cost":
                cost = parse_money(request.form.get("cost_each"))
                conn.execute(f"UPDATE sales SET cost=? WHERE line_item_id IN ({ph})", [cost] + ids)
                msg = f"Price paid set on {len(ids)} sale(s)."
            elif action == "handling":
                cost = parse_money(request.form.get("cost_each"))
                conn.execute(f"UPDATE sales SET handling_cost=? WHERE line_item_id IN ({ph})", [cost] + ids)
                msg = f"Handling (labour) set on {len(ids)} sale(s)."
            elif action == "packaging":
                cost = parse_money(request.form.get("cost_each"))
                conn.execute(f"UPDATE sales SET packaging_cost=? WHERE line_item_id IN ({ph})", [cost] + ids)
                msg = f"Packaging cost set on {len(ids)} sale(s)."
            else:
                msg = "Nothing done."
            conn.commit()
    finally:
        conn.close()
    flash(msg, "ok")
    if action == "bulk" and request.form.get("bulk_buy_id") == "new":
        return redirect(url_for("bulk_detail", bulk_id=bulk_id))
    return redirect(back)


@app.route("/export/sales.csv")
@login_required
def export_csv():
    """Exports the sales currently shown: same period (including saved ranges), search and filters."""
    from common import period_label
    key = request.args.get("period", "all")
    start, end = period_range(key, request.args.get("start"), request.args.get("end"))
    label = period_label(key, request.args.get("start"), request.args.get("end"))
    extra, extra_p = [], []
    q = (request.args.get("q") or "").strip()
    if q:
        extra.append("(s.title LIKE ? OR s.order_id LIKE ? OR s.legacy_item_id LIKE ? OR s.sku LIKE ?)")
        extra_p += [f"%{q}%"] * 4
    bulk = request.args.get("bulk", "")
    if bulk == "none":
        extra.append("s.bulk_buy_id IS NULL")
    elif bulk:
        extra.append("s.bulk_buy_id = ?")
        extra_p.append(parse_int(bulk))
    if request.args.get("filter") == "missing_cost":
        extra.append("s.bulk_buy_id IS NULL AND s.cost IS NULL")
    where, params = sales_where(start, end, " AND ".join(extra))
    conn = connect()
    try:
        sales = profit.compute_sales(conn, where, tuple(params + extra_p))
    finally:
        conn.close()
    tot = profit.summarise(sales)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"Sales — {label}"])
    w.writerow([f"{tot['count']} sales", f"Buyers paid {fmt_money(tot['gross'])}", f"eBay took {fmt_money(tot['ebay'])}",
                f"Money made {fmt_money(tot['money_made'])}"])
    w.writerow([])
    w.writerow(["Sold", "Marketplace", "Order", "Item ID", "Title", "Qty", "Buyer paid", "eBay / platform deductions",
                "Net received", "Your costs", "Money made", "Price paid", "Profit", "Bulk buy", "Fees estimated"])
    for s in sales:
        w.writerow([s["sold_at"][:10] if s["sold_at"] else "", s.get("channel") or "eBay", s["order_id"],
                    s["legacy_item_id"], s["title"], s["quantity"], s["gross"], s["ebay_deductions_total"],
                    s["net_from_ebay"], s["own_costs_total"], s["money_made"],
                    "" if s["item_cost"] is None else s["item_cost"],
                    "" if s["profit"] is None else s["profit"], s["bulk_name"] or "",
                    "yes" if s["estimated"] else ""])
    fname = "sales_" + re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_").lower() + ".csv"
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


@app.route("/ranges/save", methods=["POST"])
@login_required
def range_save():
    """Save the custom dates as a named range that appears next to the built-in periods."""
    import json
    import uuid as _uuid
    from common import saved_ranges
    name = (request.form.get("range_name") or "").strip()[:40]
    start, end = request.form.get("start") or "", request.form.get("end") or ""
    back = urlparse(request.referrer or "/").path or "/"
    if not name or not (start or end):
        flash("Pick the dates and type a name to save a range.", "error")
        return redirect(back)
    ranges = [r for r in saved_ranges() if r["name"].lower() != name.lower()]
    rid = _uuid.uuid4().hex[:8]
    ranges.append({"id": rid, "name": name, "start": start, "end": end})
    Settings().set("saved_ranges", json.dumps(ranges))
    flash(f"Saved “{name}”. It now appears with the other periods on every page.", "ok")
    key = "mode" if back.startswith("/pnl") else "period"
    return redirect(f"{back}?{key}=r:{rid}")


@app.route("/ranges/delete", methods=["POST"])
@login_required
def range_delete():
    import json
    from common import saved_ranges
    rid = request.form.get("range_id")
    Settings().set("saved_ranges", json.dumps([r for r in saved_ranges() if r["id"] != rid]))
    flash("Saved range removed.", "ok")
    return redirect(urlparse(request.referrer or "/").path or "/")


# --------------------------------------------------------------------------- #
# Bulk buys
# --------------------------------------------------------------------------- #
@app.route("/bulk")
@login_required
def bulk_list():
    conn = connect()
    try:
        rows = []
        for b in conn.execute("SELECT * FROM bulk_buys ORDER BY purchased_at DESC, id DESC"):
            b = dict(b)
            bs = profit.compute_sales(conn, "s.bulk_buy_id = ?", (b["id"],))
            rows.append({**b, **profit.bulk_summary(b, bs)})
    finally:
        conn.close()
    totals = {
        "cost": sum(r["cost"] for r in rows),
        "money_made": sum(r["money_made"] for r in rows),
        "profit": sum(r["profit"] for r in rows),
    }
    return render_template("bulk_list.html", rows=rows, totals=totals, today=date.today().isoformat())


def _bulk_form_values():
    return (
        (request.form.get("name") or "").strip() or "Untitled bulk buy",
        request.form.get("purchased_at") or date.today().isoformat(),
        parse_money(request.form.get("total_cost")) or 0,
        parse_money(request.form.get("extra_costs")) or 0,
        parse_int(request.form.get("item_count")),
        request.form.get("notes") or None,
    )


@app.route("/bulk/new", methods=["POST"])
@login_required
def bulk_new():
    conn = connect()
    try:
        with write_lock():
            cur = conn.execute("""INSERT INTO bulk_buys(name, purchased_at, total_cost, extra_costs,
                                  item_count, notes, created_at, source, location) VALUES (?,?,?,?,?,?,?,?,?)""",
                               _bulk_form_values() + (db.now_iso(), request.form.get("source") or None,
                                                      request.form.get("location") or None))
            conn.commit()
            new_id = cur.lastrowid
    finally:
        conn.close()
    flash("Bulk buy created. Now assign sales to it from the Sales page.", "ok")
    return redirect(url_for("bulk_detail", bulk_id=new_id))


@app.route("/bulk/<int:bulk_id>", methods=["GET", "POST"])
@login_required
def bulk_detail(bulk_id):
    conn = connect()
    try:
        b = conn.execute("SELECT * FROM bulk_buys WHERE id=?", (bulk_id,)).fetchone()
        if not b:
            abort(404)
        if request.method == "POST":
            dates = [b["purchased_at"], request.form.get("purchased_at")] + [
                r[0] for r in conn.execute("SELECT sold_at FROM sales WHERE bulk_buy_id=?", (bulk_id,))]
            guard = closed_guard(dates, "Changing this bulk buy")
            if guard:
                return guard
            with write_lock():
                conn.execute("""UPDATE bulk_buys SET name=?, purchased_at=?, total_cost=?,
                                extra_costs=?, item_count=?, notes=?, source=?, location=?, split_method=? WHERE id=?""",
                             _bulk_form_values() + (request.form.get("source") or None,
                                                    request.form.get("location") or None,
                                                    "price" if request.form.get("split_method") == "price" else "even",
                                                    bulk_id))
                conn.commit()
            flash("Bulk buy updated.", "ok")
            return redirect(url_for("bulk_detail", bulk_id=bulk_id))
        b = dict(b)
        sales = profit.compute_sales(conn, "s.bulk_buy_id = ?", (bulk_id,))
        summ = profit.bulk_summary(b, sales)
        rules = [dict(r) for r in conn.execute(
            """SELECT r.*, (SELECT title FROM sales WHERE legacy_item_id=r.legacy_item_id LIMIT 1) AS title
               FROM listing_rules r WHERE bulk_buy_id=?""", (bulk_id,))]
        alloc = profit.bulk_allocations(conn).get(bulk_id, {})
        unit_costs = profit.item_unit_costs(conn)
        items = []
        for it in conn.execute("""SELECT i.*, (SELECT SUM(price * COALESCE(quantity_available, 1)) FROM listings l
                                               WHERE l.item_id=i.id AND l.status='active') AS list_value,
                                         (SELECT COUNT(*) FROM listings l WHERE l.item_id=i.id
                                               AND l.status='active') AS active
                                  FROM items i WHERE bulk_buy_id=? ORDER BY title""", (bulk_id,)):
            it = dict(it)
            it["unit_cost"] = unit_costs.get(it["id"])
            items.append(it)
        list_value = sum(i["list_value"] or 0 for i in items)
        # listings that could be linked to this bulk buy (not already in it), active first
        linkable = [dict(r) for r in conn.execute(
            """SELECT l.legacy_item_id, l.title, l.price, l.status, l.start_time, l.quantity_available,
                      i.bulk_buy_id AS cur_bulk, bb.name AS cur_bulk_name,
                      (SELECT COUNT(*) FROM sales s WHERE s.legacy_item_id = l.legacy_item_id) AS sold
               FROM listings l LEFT JOIN items i ON i.id = l.item_id LEFT JOIN bulk_buys bb ON bb.id = i.bulk_buy_id
               WHERE l.status IN ('active', 'sold', 'ended') AND COALESCE(i.bulk_buy_id, -1) <> ?
               ORDER BY (l.status = 'active') DESC, l.start_time DESC LIMIT 500""", (bulk_id,))]
        # eBay sales whose listing isn't in the listings table yet (e.g. sold before your first sync)
        linkable_sales = [dict(r) for r in conn.execute(
            """SELECT legacy_item_id, MAX(title) AS title, COUNT(*) AS sold, MAX(sold_at) AS last_sold FROM sales
               WHERE source='ebay' AND legacy_item_id IS NOT NULL AND COALESCE(bulk_buy_id, -1) <> ?
                 AND legacy_item_id NOT IN (SELECT legacy_item_id FROM listings)
               GROUP BY legacy_item_id ORDER BY last_sold DESC LIMIT 300""", (bulk_id,))]
        # cumulative money made over time for the break-even chart
        running, curve = 0.0, []
        for s in sorted(sales, key=lambda s: s["sold_at"] or ""):
            running += s["money_made"]
            curve.append({"d": (s["sold_at"] or "")[:10], "v": round(running, 2)})
    finally:
        conn.close()
    return render_template("bulk_detail.html", b=b, sales=sales, summ=summ, rules=rules, curve=curve,
                           items=items, alloc=alloc, list_value=list_value, linkable=linkable,
                           linkable_sales=linkable_sales)


@app.route("/bulk/<int:bulk_id>/link-listings", methods=["POST"])
@login_required
def bulk_link_listings(bulk_id):
    """Put existing eBay listings (and their past and future sales) into this bulk buy."""
    ids = [i.strip() for i in request.form.getlist("listing_ids") if i.strip()]
    if not ids:
        flash("Tick at least one listing.", "error")
        return redirect(url_for("bulk_detail", bulk_id=bulk_id))
    conn = connect()
    try:
        b = conn.execute("SELECT * FROM bulk_buys WHERE id=?", (bulk_id,)).fetchone()
        if not b:
            abort(404)
        ph = ",".join("?" * len(ids))
        dates = [b["purchased_at"]] + [r[0] for r in conn.execute(
            f"SELECT sold_at FROM sales WHERE legacy_item_id IN ({ph})", ids)]
        guard = closed_guard(dates, f"Linking {len(ids)} listing(s) to this bulk buy")
        if guard:
            return guard
        moved = 0
        with write_lock():
            for lid in ids:
                L = conn.execute("SELECT * FROM listings WHERE legacy_item_id=?", (lid,)).fetchone()
                item_id = L["item_id"] if L else None
                if item_id is None:
                    # also covers sales of a listing that ended before your first sync
                    sold = conn.execute("""SELECT item_id, title, SUM(quantity) q FROM sales WHERE legacy_item_id=?
                                           GROUP BY legacy_item_id""", (lid,)).fetchone()
                    item_id = sold["item_id"] if sold else None
                    if item_id is None:
                        title = (L["title"] if L else None) or (sold["title"] if sold else f"Listing #{lid}")
                        qty = (L["quantity"] if L else None) or (sold["q"] if sold else 1) or 1
                        item_id = conn.execute(
                            """INSERT INTO items(title, quantity, created_at, updated_at) VALUES (?,?,?,?)""",
                            (title, qty, db.now_iso(), db.now_iso())).lastrowid
                        db.add_event(conn, item_id, "created", f"Added from listing #{lid}", lid)
                    if L:
                        conn.execute("UPDATE listings SET item_id=? WHERE legacy_item_id=?", (item_id, lid))
                # the item joins the lot; fill in where/when it was bought if blank
                conn.execute("""UPDATE items SET bulk_buy_id=?, source=COALESCE(source, ?), location=COALESCE(location, ?),
                                purchased_at=COALESCE(purchased_at, ?), updated_at=? WHERE id=?""",
                             (bulk_id, b["source"], b["location"], b["purchased_at"], db.now_iso(), item_id))
                # every relist of this item, plus past sales, follow it
                conn.execute("""UPDATE sales SET item_id=?, bulk_buy_id=? WHERE legacy_item_id=? OR item_id=?""",
                             (item_id, bulk_id, lid, item_id))
                conn.execute("""INSERT INTO listing_rules(legacy_item_id, bulk_buy_id, unit_cost) VALUES (?,?,NULL)
                                ON CONFLICT(legacy_item_id) DO UPDATE SET bulk_buy_id=excluded.bulk_buy_id""",
                             (lid, bulk_id))
                db.add_event(conn, item_id, "linked", f"Added to bulk buy “{b['name']}”", lid)
                moved += 1
            conn.commit()
    finally:
        conn.close()
    flash(f"Linked {moved} listing{'s' if moved != 1 else ''} to {b['name']}. Their sales now count towards it.", "ok")
    return redirect(url_for("bulk_detail", bulk_id=bulk_id))


@app.route("/bulk/<int:bulk_id>/delete", methods=["POST"])
@login_required
def bulk_delete(bulk_id):
    conn = connect()
    try:
        b = conn.execute("SELECT purchased_at FROM bulk_buys WHERE id=?", (bulk_id,)).fetchone()
        dates = ([b["purchased_at"]] if b else []) + [
            r[0] for r in conn.execute("SELECT sold_at FROM sales WHERE bulk_buy_id=?", (bulk_id,))]
        guard = closed_guard(dates, "Deleting this bulk buy")
        if guard:
            return guard
        with write_lock():
            conn.execute("UPDATE items SET bulk_buy_id=NULL WHERE bulk_buy_id=?", (bulk_id,))
            conn.execute("UPDATE sales SET bulk_buy_id=NULL WHERE bulk_buy_id=?", (bulk_id,))
            conn.execute("DELETE FROM listing_rules WHERE bulk_buy_id=?", (bulk_id,))
            conn.execute("DELETE FROM bulk_buys WHERE id=?", (bulk_id,))
            conn.commit()
    finally:
        conn.close()
    flash("Bulk buy deleted (its sales were kept).", "ok")
    return redirect(url_for("bulk_list"))


# --------------------------------------------------------------------------- #
# Sold price tracking
# --------------------------------------------------------------------------- #
@app.route("/prices")
@login_required
def prices():
    q = (request.args.get("q") or "").strip()
    conn = connect()
    try:
        where, params = "1=1", ()
        if q:
            where, params = "(s.title LIKE ? OR s.legacy_item_id LIKE ?)", (f"%{q}%", f"%{q}%")
        sales = profit.compute_sales(conn, where, params)
    finally:
        conn.close()
    groups = {}
    for s in sales:
        key = s["legacy_item_id"] or s["title"]
        g = groups.setdefault(key, {"key": key, "title": s["title"], "sales": [], "units": 0})
        g["sales"].append(s)
        g["units"] += s["quantity"] or 1
    rows = []
    for g in groups.values():
        unit_prices = [s["unit_price"] for s in g["sales"]]
        unit_made = [s["money_made"] / max(s["quantity"] or 1, 1) for s in g["sales"]]
        latest = max(g["sales"], key=lambda s: s["sold_at"] or "")
        hist = sorted(g["sales"], key=lambda s: s["sold_at"] or "")
        rows.append({
            "key": g["key"], "title": latest["title"], "times": len(g["sales"]), "units": g["units"],
            "avg": sum(unit_prices) / len(unit_prices), "min": min(unit_prices), "max": max(unit_prices),
            "avg_made": sum(unit_made) / len(unit_made), "last": latest["unit_price"],
            "last_at": latest["sold_at"], "spark": [s["unit_price"] for s in hist][-20:],
        })
    sort = request.args.get("sort", "recent")
    keyf = {"recent": lambda r: r["last_at"] or "", "sold": lambda r: r["units"],
            "avg": lambda r: r["avg"], "title": lambda r: (r["title"] or "").lower()}[
        sort if sort in ("recent", "sold", "avg", "title") else "recent"]
    rows.sort(key=keyf, reverse=sort != "title")
    return render_template("prices.html", rows=rows, q=q, sort=sort)


@app.route("/prices/<path:key>")
@login_required
def price_detail(key):
    conn = connect()
    try:
        sales = profit.compute_sales(conn, "(s.legacy_item_id = ? OR (s.legacy_item_id IS NULL AND s.title = ?))",
                                     (key, key))
    finally:
        conn.close()
    if not sales:
        abort(404)
    hist = sorted(sales, key=lambda s: s["sold_at"] or "")
    return render_template("price_detail.html", sales=sales, hist=hist, key=key, title=hist[-1]["title"],
                           chart_dates=[(s["sold_at"] or "")[:10] for s in hist])


# --------------------------------------------------------------------------- #
# Settings & sync
# --------------------------------------------------------------------------- #
def _fy_start_value(m, d):
    try:
        m, d = int(m), int(d)
        date(2001, m, d)
        return f"{m:02d}-{d:02d}"
    except (TypeError, ValueError):
        return "04-06"


def _money_str(v):
    m = parse_money(v)
    return None if m is None else f"{m:.2f}"


@app.route("/settings/update/check", methods=["POST"])
@login_required
def update_check():
    st = updater.check()
    if st.get("error"):
        flash(st["error"], "error")
    elif updater.switching():
        flash(f"Click Update now to switch to the {'beta' if updater.channel() == 'beta' else 'main release'} "
              f"(version {st['latest']}).", "ok")
    elif updater.is_newer(st.get("latest")):
        flash(f"Version {st['latest']} is available (you have {updater.current_version()}).", "ok")
    else:
        flash(f"You're on the latest version ({updater.current_version()}).", "ok")
    return redirect(url_for("settings_page") + "#updates")


@app.route("/settings/market", methods=["POST"])
@login_required
def settings_market():
    site = request.form.get("marketplace_id")
    cur = request.form.get("currency") or ""
    if site not in markets.SITES or (cur and cur not in markets.CURRENCIES):
        abort(400)
    st = Settings()
    before = markets.site_id()
    st.set_many({"marketplace_id": site, "currency": cur or None})
    if site != before:
        st.set("fy_start", None)          # back to the new market's tax year
    m = markets.site()
    flash(f"Now set up for {m['name']}: money in {markets.currency()}, distances in {markets.unit_word()}"
          + (f", {markets.tax()['name']} summary under Accounting." if markets.tax() else "."), "ok")
    return redirect(url_for("settings_page") + "#market")


@app.route("/settings/update/channel", methods=["POST"])
@login_required
def update_channel():
    ch = request.form.get("channel")
    if ch not in updater.CHANNELS:
        abort(400)
    Settings().set("update_channel", ch)
    return update_check()


@app.route("/settings/update/apply", methods=["POST"])
@login_required
def update_apply():
    if app.config["DEMO"]:
        flash("Updating is turned off in demo mode.", "error")
        return redirect(url_for("settings_page") + "#updates")
    if sync.STATUS.get("running") or automation.STATE.get("running"):
        flash("A sync or automation is running. Try again in a minute.", "error")
        return redirect(url_for("settings_page") + "#updates")
    try:
        msg = updater.apply_update()
    except Exception as e:  # noqa: BLE001
        flash(f"Update failed, nothing was changed in your data: {e}", "error")
        return redirect(url_for("settings_page") + "#updates")
    updater.restart()
    return render_template("restarting.html", message=msg)


@app.route("/settings/database/download")
@login_required
def database_download():
    """A consistent copy of the whole database (sales, costs, settings, eBay keys) to keep safe."""
    import sqlite3
    tmp = Path(tempfile.mkdtemp()) / f"flip-for-free-{date.today():%Y-%m-%d}.db"
    src, dst = sqlite3.connect(db.get_db_path()), sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    from flask import send_file
    return send_file(tmp, as_attachment=True, download_name=tmp.name, mimetype="application/x-sqlite3")


@app.route("/settings/database/restore", methods=["POST"])
@login_required
def database_restore():
    """Replace the current data with a downloaded copy (the current data is backed up first)."""
    import sqlite3
    f = request.files.get("file")
    if not f or not f.filename:
        flash("Choose a Flip for Free database file (.db) to restore.", "error")
        return redirect(url_for("settings_page") + "#database")
    tmp = Path(tempfile.mkdtemp()) / "restore.db"
    f.save(tmp)
    if not db.is_tracker_db(str(tmp)):
        flash("That file isn't a Flip for Free database.", "error")
        return redirect(url_for("settings_page") + "#database")
    if sync.STATUS.get("running") or automation.STATE.get("running"):
        flash("A sync or automation is running. Try again in a minute.", "error")
        return redirect(url_for("settings_page") + "#database")
    saved = updater.backup_database("before-restore")
    with write_lock():
        src, dst = sqlite3.connect(tmp), sqlite3.connect(db.get_db_path())
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    db._initialised.discard(db.get_db_path())
    db.ensure_schema()
    EbayClient.forget_tokens()
    session.clear()
    flash(f"Restored. Your previous data was saved as {saved.name if saved else 'a backup'} in the backups folder "
          "next to your database. Sign in again.", "ok")
    return redirect(url_for("login"))


@app.route("/settings/api-keys", methods=["POST"])
@login_required
def api_keys():
    if request.form.get("revoke"):
        api.revoke_key(request.form["revoke"])
        flash("API key revoked. Anything using it stops working straight away.", "ok")
        return redirect(url_for("settings_page") + "#api")
    key = api.create_key(request.form.get("name") or "Claude")
    flash("API key created. Copy it now: it won't be shown again.", "ok")
    # show the key on this one page only (it is never stored in the session or database)
    g.new_key = key
    return settings_page()


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings_page():
    st = Settings()
    if request.method == "POST" and request.endpoint == "settings_page":
        st.set_many({
            "include_account_fees": "1" if request.form.get("include_account_fees") else "0",
            "sync_interval_minutes": str(min(max(parse_int(request.form.get("sync_interval_minutes")) or 0, 0), 1440)
                                         if (parse_int(request.form.get("sync_interval_minutes")) or 0) in
                                         dict(SYNC_CHOICES) else 360),
            "auto_update": "1" if request.form.get("auto_update") else "0",
            "first_sync_days": str(min(max(parse_int(request.form.get("first_sync_days")) or 730, 7), 1825)),
            "profit_basis": "sold" if request.form.get("profit_basis") == "sold" else "bought",
            "fy_start": _fy_start_value(request.form.get("fy_month"), request.form.get("fy_day")),
            "accountant_email": (request.form.get("accountant_email") or "").strip() or None,
            "default_packaging": _money_str(request.form.get("default_packaging")),
            "default_handling": _money_str(request.form.get("default_handling")),
            "labour_minutes": request.form.get("labour_minutes") or None,
            "labour_rate": request.form.get("labour_rate") or None,
            "labour_paid": "1" if request.form.get("labour_paid") else "0",
            "packing_defaults_from": request.form.get("packing_defaults_from") or date.today().isoformat(),
        })
        flash("Settings saved.", "ok")
        return redirect(url_for("settings_page"))
    s = st.get_many(["include_account_fees", "auto_sync_hours", "sync_interval_minutes", "auto_update", "first_sync_days", "owner_username", "profit_basis",
                     "fy_start", "accountant_email", "default_packaging", "default_handling", "labour_minutes",
                     "labour_rate", "labour_paid", "packing_defaults_from",
                     "ebay_env", "client_id", "ru_name", "marketplace_id", "refresh_token_expiry",
                     "last_sync_orders", "last_sync_finances", "signing_jwe"])
    conn = connect()
    try:
        log = [dict(r) for r in conn.execute("SELECT * FROM sync_log ORDER BY id DESC LIMIT 10")]
        counts = {
            "sales": conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0],
            "orders": conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
            "transactions": conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0],
            "bulk": conn.execute("SELECT COUNT(*) FROM bulk_buys").fetchone()[0],
        }
    finally:
        conn.close()
    return render_template("settings.html", api_keys=api.list_keys(), new_key=g.get("new_key"),
                           sync_choices=SYNC_CHOICES, sync_minutes=sync_interval_minutes(st),
                           version=updater.current_version(), upd=updater.STATE, repo=updater.repo(),
                           update_available=updater.update_available(), channel=updater.channel(),
                           sites=markets.SITES, currencies=markets.CURRENCIES,
                           fy_default="%02d-%02d" % markets.year_start(),
                           fy_default_label="{0.day} {0:%B}".format(date(2001, *markets.year_start())),
                           installed_channel=updater.installed_channel(),
                           server_mode=bool(app.config.get("PUBLIC_URL")),
                           api_base=(app.config.get("PUBLIC_URL") or f"{request.scheme}://{request.host}") + api.PREFIX,
                           today=date.today().isoformat(), s=s, log=log, counts=counts, cfg=db.load_config(),
                           config_dir=str(db.config_dir()), device=db.device_name())


@app.route("/sync", methods=["POST"])
@login_required
def sync_now():
    if app.config["DEMO"]:
        return jsonify({"started": False, "message": "Sync is disabled in demo mode."})
    started = sync.start_background(full=bool(request.form.get("full")))
    return jsonify({"started": started, "message": "Sync started." if started else "A sync is already running."})


@app.route("/sync/status")
@login_required
def sync_status():
    return jsonify(sync.STATUS)


SYNC_CHOICES = [(0, "Off"), (5, "Every 5 minutes"), (10, "Every 10 minutes"), (15, "Every 15 minutes"),
                (30, "Every 30 minutes"), (60, "Every hour"), (120, "Every 2 hours"), (360, "Every 6 hours"),
                (720, "Every 12 hours"), (1440, "Once a day")]


def sync_interval_minutes(st=None) -> int:
    st = st or Settings()
    v = st.get("sync_interval_minutes")
    if v is None:   # older setting was in hours
        v = (parse_int(st.get("auto_sync_hours", "6")) or 0) * 60
    return parse_int(v) or 0


def _background_error(where, e):
    """Log a background job's error and leave a notification with a way to report it."""
    err = errors.record(where, e)
    try:
        if db_ready():
            conn = db.connect()
            try:
                with db.write_lock():
                    errors.notify_failure(conn, where, err)
                    conn.commit()
            finally:
                conn.close()
    except Exception:  # noqa: BLE001
        errors.log.exception("couldn't store the error notification")


def setup_stage():
    """What's left to set up: 'database', 'ebay_keys', 'sign_in' or None when ready."""
    try:
        if not db_ready():
            return "database"
        st = Settings()
        if not (st.get("client_id") and st.get("client_secret") and st.get("ru_name")):
            return "ebay_keys"
        if not st.get("refresh_token"):
            return "sign_in"
    except Exception:  # noqa: BLE001
        return "database"
    return None


def _announce_ready(url, args, ip=None):
    """Wait until the web server answers, then tell the person in the terminal where to go next."""
    import socket
    for _ in range(150):
        try:
            with socket.create_connection(("127.0.0.1", args.port), timeout=1):
                break
        except OSError:
            time.sleep(0.2)
    stage = "demo" if app.config.get("DEMO") else setup_stage()
    where = "on this computer, " if (args.lan and ip) else ""
    addr = "the first address" if (args.lan and ip) else "the address"
    nxt = {
        "database": f"Next, {where}go to {addr} to finish setting up: choose where to keep your data,\n"
                    "  add your eBay developer keys and sign in with eBay.",
        "ebay_keys": f"Next, {where}go to {addr} to finish setting up: add your eBay developer keys,\n"
                     "  then sign in with eBay.",
        "sign_in": f"Next, {where}go to {addr} and click Sign in with eBay to finish setting up.",
        "demo": "Demo mode: go to the address to look around with made-up data.",
        None: "Everything's set up. Go to the address in your browser.",
    }[stage]
    line = "=" * 70
    lines = ["", line, "  Flip for Free is ready.", ""]
    if app.config.get("PUBLIC_URL"):
        lines += [f"  Go to:  {url}"]
    elif args.lan:
        net = f"{app.config['SCHEME']}://{ip}:{args.port}/" if ip else None
        lines += [f"  On this computer:      {url}"]
        if net:
            lines += [f"  On other devices:      {net}",
                      "",
                      "  The second address is for your phone, tablet or another computer, and it ONLY works",
                      "  on devices connected to the same Wi-Fi or network as this computer. It won't work",
                      "  over mobile data or from anywhere else. Each device must be paired once first: on",
                      "  this computer open Settings → Use on your phone and scan the code."]
        else:
            lines += ["  (Couldn't find this computer's network address, so only this computer can use it.)"]
    else:
        lines += [f"  Go to:  {url}", "", "  Only this computer can reach it (started with --local-only)."]
    lines += ["", f"  {nxt}"]
    if app.config["SCHEME"] == "https" and not app.config.get("PUBLIC_URL"):
        lines += ["  The first time, your browser warns about the certificate. That's expected:",
                  "  choose Advanced, then Proceed (or Show details, then Visit this website)."]
    if args.lan and ip and sys.platform.startswith("win"):
        lines += ["  If Windows asks whether Python may use the network, allow Private networks only."]
    if not args.no_browser:
        lines += ["", "  Opening it in your browser now…"]
    lines += ["", "  Keep this window open while you use Flip for Free. Ctrl+C stops it.", line, ""]
    print("\n".join(lines), flush=True)
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass


def auto_sync_loop():
    time.sleep(30)
    while True:
        try:
            if not app.config["DEMO"] and db_ready():
                st = Settings()
                mins = sync_interval_minutes(st)
                if mins and st.get("refresh_token"):
                    last = st.get("last_sync_orders")
                    age = ((datetime.now(timezone.utc) - datetime.fromisoformat(last.replace("Z", "+00:00")))
                           .total_seconds() if last else 10 ** 9)
                    if age >= mins * 60 - 20:
                        sync.start_background()
        except Exception as e:  # noqa: BLE001
            _background_error("Automatic sync", e)
        time.sleep(60)


def update_loop():
    """Check GitHub for a new version once a day; install it automatically if you've switched that on."""
    time.sleep(120)
    while True:
        try:
            if db_ready() and not app.config["DEMO"]:
                st = updater.check()
                if updater.is_newer(st.get("latest")) and Settings().get("auto_update") == "1" \
                        and not sync.STATUS.get("running") and not automation.STATE.get("running"):
                    print(updater.apply_update())
                    updater.restart()
        except Exception as e:  # noqa: BLE001
            errors.record("Update check", e)          # usually just offline; logged, not notified
        time.sleep(24 * 3600)


def automation_loop():
    time.sleep(60)
    while True:
        try:
            if db_ready() and (app.config["DEMO"] or Settings().get("refresh_token")):
                db.ensure_schema()
                msg = automation.run_all(simulate=app.config["DEMO"])
                print("automations:", msg)
        except Exception as e:  # noqa: BLE001
            _background_error("Automations", e)
        time.sleep(30 * 60)


import features  # noqa: E402  (registers inventory, insights, automations, tax, notifications)
import features2  # noqa: E402  (P&L, receipts, import wizard, performance, tools, phone access)
features.register(app)
features2.register(app)

import api  # noqa: E402  JSON API for Claude and other tools
api.register(app)

import errors  # noqa: E402  log file, error page, GitHub issue reports
errors.register(app)
import icons  # noqa: E402  one SVG icon set for the templates
icons.register(app)
import crosslist  # noqa: E402  copy listings to Vinted by hand; keep eBay in step with sales elsewhere
crosslist.register(app)
import stores  # noqa: E402  CardTrader and Shopify: listing, order import, stock in step
stores.register(app)

LOCAL_ADDRS = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
# In Docker, requests from your own computer arrive from Docker's own network, not 127.0.0.1.
# docker-compose.yml publishes the port on 127.0.0.1 only, so these count as "this computer".
DOCKER_NETS = ("172.16.0.0/12", "192.168.65.0/24", "fd00::/8")


def is_local(addr):
    if addr in LOCAL_ADDRS:
        return True
    if app.config.get("DOCKER") and addr:
        import ipaddress
        try:
            ip = ipaddress.ip_address(addr.split("%")[0])
            return any(ip in ipaddress.ip_network(n) for n in DOCKER_NETS)
        except ValueError:
            return False
    return False
OPEN_ENDPOINTS = {"pair", "static", "manifest"}


@app.before_request
def lan_guard():
    """In --lan mode only paired, signed-in devices may use the app from the network.
    Setup, eBay sign-in and the folder browser stay on this computer."""
    if is_local(request.remote_addr) or request.endpoint in OPEN_ENDPOINTS:
        return None
    if app.config.get("PUBLIC_URL"):
        # Server mode: Flip listens on 127.0.0.1 only and the proxy (Tailscale Serve / Cloudflare) decides who gets
        # in. ProxyFix turns remote_addr into the visitor's own address, so it never looks local here.
        return None
    if not app.config.get("LAN"):
        abort(403)
    if (request.endpoint or "").startswith("api_v1"):
        return None          # the API checks its own key
    if not session.get("user_id") or request.endpoint in ("setup", "setup_db", "setup_ebay", "api_browse", "login",
                                                          "auth_start", "auth_callback", "auth_paste", "phone"):
        return render_template("pair_failed.html"), 403
    return None


# --------------------------------------------------------------------------- #
# HTTPS for localhost
# --------------------------------------------------------------------------- #
def ensure_cert(extra_ip=None):
    d = db.config_dir()
    tag = "lan" if extra_ip else "localhost"
    cert, key = d / f"{tag}-cert.pem", d / f"{tag}-key.pem"
    if cert.exists() and key.exists():
        if not extra_ip or (d / "lan-ip.txt").exists() and (d / "lan-ip.txt").read_text() == extra_ip:
            return str(cert), str(key)
    import ipaddress
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    c = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key())
         .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
         .not_valid_after(now + timedelta(days=3650))
         .add_extension(x509.SubjectAlternativeName(
             [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
             + ([x509.IPAddress(ipaddress.ip_address(extra_ip))] if extra_ip else [])), critical=False)
         .sign(k, hashes.SHA256()))
    cert.write_bytes(c.public_bytes(serialization.Encoding.PEM))
    if extra_ip:
        (d / "lan-ip.txt").write_text(extra_ip)
    key.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                    serialization.NoEncryption()))
    return str(cert), str(key)


def main():
    if os.environ.get("FLIP_START_DELAY"):          # restarting after an update: let the old process exit
        time.sleep(float(os.environ.pop("FLIP_START_DELAY")))
    ap = argparse.ArgumentParser(description="Flip for Free")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-https", action="store_true", help="serve plain http")
    ap.add_argument("--demo", action="store_true", help="run with fake data (no eBay account)")
    ap.add_argument("--market", default="", help="with --demo: pretend to be on this eBay site, e.g. EBAY_US")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--public-url", default=os.environ.get("FLIP_PUBLIC_URL", ""),
                    help="server mode: the https address people use, e.g. https://flip.tailnet.ts.net "
                         "(Flip runs on plain http behind Tailscale/Cloudflare, which add the https)")
    ap.add_argument("--local-only", action="store_true",
                    help="only this computer can reach Flip for Free (turns --lan off)")
    ap.add_argument("--docker", action="store_true", default=os.environ.get("FLIP_DOCKER") == "1",
                    help="running in a Docker container: listen on all interfaces, trust Docker's own network")
    ap.add_argument("--lan", action="store_true",
                    help="also allow your phone on the same Wi-Fi (paired devices only; see Settings → Use on your phone)")
    args = ap.parse_args()

    if args.demo:
        tmp = Path(tempfile.mkdtemp(prefix="ept-demo-"))
        os.environ["EPT_CONFIG_DIR"] = str(tmp)
        import demo
        path = str(tmp / db.DB_FILENAME)
        demo.seed(path)
        db.set_db_path(path)
        if args.market:
            if args.market.upper() not in markets.SITES:
                raise SystemExit(f"--market must be one of: {', '.join(markets.SITES)}")
            Settings().set("marketplace_id", args.market.upper())
            c = db.connect()          # the demo text was written in pounds; show it in this market's currency
            for t, col in (("item_events", "detail"), ("notifications", "body"), ("automation_log", "detail")):
                c.execute(f"UPDATE {t} SET {col} = REPLACE({col}, '£', ?)", (markets.symbol(),))
            c.commit()
            c.close()
        app.config["DEMO"] = True
        print(f"Demo database: {path}")

    app.config["PORT"] = args.port
    public = args.public_url.strip().rstrip("/")
    if public:
        # Server mode: a proxy on this machine (Tailscale Serve / Cloudflare Tunnel) provides https.
        if not public.startswith("https://"):
            sys.exit("--public-url must start with https://")
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
        app.config["PUBLIC_URL"] = public
        args.no_https, args.no_browser = True, True
    app.config["SCHEME"] = "http" if args.no_https else "https"
    app.secret_key = db.load_config()["secret_key"]
    app.config.update(SESSION_COOKIE_SECURE=bool(public) or not args.no_https, SESSION_COOKIE_SAMESITE="Lax",
                      PERMANENT_SESSION_LIFETIME=timedelta(days=7))
    threading.Thread(target=auto_sync_loop, daemon=True).start()
    threading.Thread(target=automation_loop, daemon=True).start()
    threading.Thread(target=update_loop, daemon=True).start()

    if args.local_only:
        args.lan = False
    app.config["LAN"] = args.lan
    app.config["DOCKER"] = args.docker
    if args.docker:
        args.no_browser = True
    url = (public + "/") if public else f"{app.config['SCHEME']}://localhost:{args.port}/"
    ip = features2.lan_ip() if args.lan else None
    ssl_ctx = None if args.no_https else ensure_cert(ip)
    # Keep the terminal readable: our own "ready" message instead of the web server's developer banner.
    import logging
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    try:
        import flask.cli
        flask.cli.show_server_banner = lambda *a, **k: None
    except Exception:  # noqa: BLE001
        pass
    threading.Thread(target=_announce_ready, args=(url, args, ip), daemon=True).start()
    app.run(host="0.0.0.0" if (args.lan or args.docker) else "127.0.0.1", port=args.port, ssl_context=ssl_ctx, debug=False,
            threaded=True)


if __name__ == "__main__":
    main()
