"""Flip for Free JSON API - lets Claude (or any script) read and update your data.

Every request needs an API key from Settings -> API keys:
    Authorization: Bearer flip_xxxxxxxx

All money is in GBP, dates are YYYY-MM-DD. Errors come back as {"error": "..."} with an HTTP status.
A change that would alter a tax year you've marked as filed returns 409 unless you send
"confirm_closed": true. GET /api/v1 describes every endpoint.
"""
import hashlib
import json
import secrets
import uuid
from datetime import date
from functools import wraps

from flask import jsonify, request

import db
import profit
import taxcalc
from common import closed_years, parse_int, parse_money, period_range, sales_where, tax_year_label, tax_year_of
from db import Settings, add_event, connect, now_iso, write_lock
from markets import money as fmt_money

PREFIX = "/api/v1"


# --------------------------------------------------------------------------- #
# Keys
# --------------------------------------------------------------------------- #
def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def list_keys() -> list[dict]:
    try:
        return json.loads(Settings().get("api_keys") or "[]")
    except ValueError:
        return []


def _save_keys(keys):
    Settings().set("api_keys", json.dumps(keys))


def create_key(name: str) -> str:
    """Returns the full key ONCE; only its hash is stored."""
    key = "flip_" + secrets.token_urlsafe(32)
    keys = list_keys()
    keys.append({"id": uuid.uuid4().hex[:8], "name": (name or "Claude")[:40], "hash": _hash(key),
                 "hint": key[:9] + "…" + key[-4:], "created": now_iso(), "last_used": None})
    _save_keys(keys)
    return key


def revoke_key(key_id: str):
    _save_keys([k for k in list_keys() if k["id"] != key_id])


def _check_key():
    auth = request.headers.get("Authorization", "")
    key = auth[7:].strip() if auth.lower().startswith("bearer ") else request.headers.get("X-API-Key", "").strip()
    if not key:
        return None
    h = _hash(key)
    keys = list_keys()
    for k in keys:
        if secrets.compare_digest(k["hash"], h):
            # record use at most every 10 minutes
            if not k.get("last_used") or k["last_used"][:15] != now_iso()[:15]:
                k["last_used"] = now_iso()
                _save_keys(keys)
            return k
    return None


def api_auth(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not db.get_db_path():
            return _err("Flip for Free isn't set up yet: choose a database on the setup page first.", 503)
        db.ensure_schema()
        if not _check_key():
            return _err("Missing or invalid API key. Create one in Settings → API keys and send it as "
                        "'Authorization: Bearer <key>'.", 401)
        try:
            return f(*a, **kw)
        except ApiError as e:
            return _err(str(e), e.status)
    return wrapper


class ApiError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


def _err(msg, status):
    return jsonify({"error": msg}), status


def _body() -> dict:
    data = request.get_json(silent=True)
    if data is None and request.form:
        data = request.form.to_dict()
    return data or {}


def _money(v, field):
    if v is None or v == "":
        return None
    m = parse_money(v)
    if m is None:
        raise ApiError(f"'{field}' must be an amount like 4.50")
    return m


def _date(v, field, default=None):
    if not v:
        return default
    try:
        return date.fromisoformat(str(v)[:10]).isoformat()
    except ValueError:
        raise ApiError(f"'{field}' must be a date like 2026-11-21")


def _guard_closed(dates, body):
    hit = sorted({y for y in (tax_year_of(d) for d in dates if d) if y in closed_years()})
    if hit and not body.get("confirm_closed"):
        raise ApiError("This changes a tax year marked as filed (" + ", ".join(tax_year_label(y) for y in hit) +
                       "). Send \"confirm_closed\": true if you really mean to change it.", 409)


def _sale_json(s):
    keep = ["line_item_id", "order_id", "legacy_item_id", "title", "quantity", "channel", "sold_at", "status",
            "gross", "ebay_deductions", "ebay_deductions_total", "net_from_ebay", "own_costs", "own_costs_total",
            "money_made", "item_cost", "cost_source", "profit", "bulk_buy_id", "bulk_name", "item_id",
            "item_location", "estimated", "source"]
    out = {k: s.get(k) for k in keep}
    out["missing_cost"] = s["item_cost"] is None and not s["in_bulk"]
    return out


def _item_json(it):
    keep = ["id", "title", "sku", "quantity", "remaining", "status", "cost", "unit_cost", "cost_locked", "source",
            "purchased_at", "location", "bulk_buy_id", "bulk_name", "list_price", "active_id", "days_listed",
            "sold_units", "packaging_cost", "handling_cost", "restock_url", "notes", "written_off_at"]
    return {k: it.get(k) for k in keep}


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
ENDPOINTS = [
    ("GET", "/summary", "Profit summary for a period. ?period=today|7d|30d|month|last_month|90d|ytd|fy|all|custom"
                        "&start=&end= (custom), or a saved range 'r:<id>'."),
    ("GET", "/time-to-sell", "Time to sell. Single-quantity listings: average/median days from first listed to sold. "
                             "Multi-quantity listings: how often one sells ('one every N days'), stock left and "
                             "sell-out estimate. ?period=…&start=&end="),
    ("GET", "/sales", "Sales with full breakdown. ?period=…&q=search&missing_cost=1&channel=Vinted&limit=200"),
    ("GET", "/sales/<line_item_id>", "One sale."),
    ("PATCH", "/sales/<line_item_id>", "Set your costs on a sale: cost, postage_cost, packaging_cost, "
                                       "handling_cost, other_cost, bulk_buy_id (null to remove), notes."),
    ("POST", "/sales", "Record a sale from another marketplace: title*, item_price*, sold_at, channel, quantity, "
                       "delivery_charged, fees, postage_cost, packaging_cost, handling_cost, cost, item_id, notes, update_ebay "
                       "(true: lower or end the item's listings on eBay, CardTrader and Shopify)."),
    ("GET", "/items", "Inventory. ?status=instock|listed|unlisted|sold|all&q=&location=&source=&missing_cost=1"),
    ("GET", "/items/<id>", "One item, with its listings, sales and timeline."),
    ("POST", "/items", "Add stock: title* (or titles: [..] for several), quantity, cost (each), source, "
                       "purchased_at, location, bulk_buy_id, cost_locked, sku, packaging_cost, handling_cost, "
                       "restock_url, notes."),
    ("PATCH", "/items/<id>", "Change any of the fields above, plus link_listing (eBay item ID)."),
    ("POST", "/items/<id>/write-off", "Write an item off: reason, date."),
    ("GET", "/bulk-buys", "Bulk buys with cost, money made back, profit and ROI."),
    ("POST", "/bulk-buys", "Create a bulk buy: name*, total_cost*, purchased_at, extra_costs, item_count, source, "
                           "location, split_method (even|price), notes, items: ['title', …] to log its items."),
    ("PATCH", "/bulk-buys/<id>", "Change a bulk buy's fields."),
    ("POST", "/bulk-buys/<id>/link-listings", "listing_ids: [eBay item IDs] - their past and future sales count "
                                              "towards this bulk buy."),
    ("GET", "/listings", "Your eBay listings. ?status=active|ended|sold|all&q="),
    ("GET", "/expenses", "?year=2026 (tax year starting 6 Apr 2026)"),
    ("POST", "/expenses", "date, category* (" + ", ".join(k for k, _, _ in taxcalc.EXPENSE_CATEGORIES) +
                          "), description*, amount*, item_id"),
    ("POST", "/mileage", "date, miles*, purpose*, vehicle (car|motorcycle|bicycle), route, return_trip (bool)"),
    ("GET", "/tax", "Self Assessment (SA103S) figures and data checks. ?year=2026&basis=cash|accrual"),
    ("GET", "/notifications", "?unread=1&kind=sale|issue|automation"),
    ("POST", "/sync", "Start a sync with eBay now."),
]


def register(app):

    @app.route(PREFIX, methods=["GET"])
    @app.route(PREFIX + "/", methods=["GET"])
    @api_auth
    def api_v1_index():
        return jsonify({"app": "Flip for Free", "auth": "Authorization: Bearer <key>", "base": PREFIX,
                        "money": "GBP", "dates": "YYYY-MM-DD",
                        "endpoints": [{"method": m, "path": PREFIX + p, "does": d} for m, p, d in ENDPOINTS]})

    # ---------------- summary ----------------
    @app.route(PREFIX + "/summary")
    @api_auth
    def api_v1_summary():
        key = request.args.get("period", "30d")
        start, end = period_range(key, request.args.get("start"), request.args.get("end"))
        conn = connect()
        try:
            where, params = sales_where(start, end)
            sales = profit.compute_sales(conn, where, tuple(params))
            tot = profit.summarise(sales)
            acct = profit.account_charges(conn, start, end)
            unlisted = conn.execute("SELECT COUNT(*) FROM items WHERE archived=0 AND id NOT IN "
                                    "(SELECT item_id FROM listings WHERE status='active' AND item_id IS NOT NULL) "
                                    "AND COALESCE((SELECT SUM(quantity) FROM sales s WHERE s.item_id=items.id),0) "
                                    "< COALESCE(quantity,1)").fetchone()[0]
            active = conn.execute("SELECT COUNT(*) FROM listings WHERE status='active'").fetchone()[0]
        finally:
            conn.close()
        cogs = tot["item_costs"] + tot["bulk_alloc"]
        return jsonify({
            "period": key, "start": start, "end": end, "sales": tot["count"], "items_sold": tot["units"],
            "buyers_paid": tot["gross"], "ebay_and_platform_deductions": tot["ebay"], "your_sale_costs": tot["own"],
            "money_made": tot["money_made"], "cost_of_goods_sold": round(cogs, 2),
            "profit_after_cost_of_goods": round(tot["money_made"] - cogs, 2),
            "account_charges": acct["total"], "deductions_breakdown": tot["deductions"],
            "sales_missing_cost": tot["missing_cost"], "sales_with_estimated_fees": tot["estimated"],
            "active_listings": active, "items_not_listed": unlisted,
        })

    @app.route(PREFIX + "/time-to-sell")
    @api_auth
    def api_v1_time_to_sell():
        import timetosell
        key = request.args.get("period", "90d")
        start, end = period_range(key, request.args.get("start"), request.args.get("end"))
        conn = connect()
        try:
            t = timetosell.compute(conn, start=start, end=end)
        finally:
            conn.close()
        r2 = lambda v: round(v, 1) if isinstance(v, float) else v  # noqa: E731
        s1, m = t["single"], t["multi"]
        return jsonify({
            "period": key, "start": start, "end": end,
            "single_quantity": {
                "sold": s1["count"], "average_days": r2(s1["avg"]), "median_days": r2(s1["median"]),
                "fastest_days": r2(s1["fastest"]), "slowest_days": r2(s1["slowest"]),
                "spread": [{"label": b["label"], "count": b["count"]} for b in s1["spread"]],
                "still_listed": s1["still_listed_count"], "still_listed_average_days": r2(s1["still_listed_avg"]),
                "longest_listed": [{"title": r["title"], "days": r2(r["days"]), "item_id": r["item_id"]}
                                   for r in s1["still_listed"][:10]]},
            "multi_quantity": {
                "listings": m["count"], "units_sold": m["units"], "one_sells_every_days": r2(m["every_days"]),
                "median_one_sells_every_days": r2(m["median_every"]), "units_per_week": r2(m["per_week"]),
                "by_listing": [{"title": r["title"], "item_id": r["item_id"], "units_sold": r["units"],
                                "one_sells_every_days": r2(r["every_days"]), "units_per_week": r2(r["per_week"]),
                                "stock_left": r["stock_left"], "active": r["active"],
                                "days_since_last_sale": r2(r["days_since_sale"]),
                                "sells_out_in_days": r2(r["sell_out_days"])} for r in m["listings"]]},
        })

    # ---------------- sales ----------------
    @app.route(PREFIX + "/sales", methods=["GET"])
    @api_auth
    def api_v1_sales():
        key = request.args.get("period", "all")
        start, end = period_range(key, request.args.get("start"), request.args.get("end"))
        extra, ep = [], []
        q = (request.args.get("q") or "").strip()
        if q:
            extra.append("(s.title LIKE ? OR s.order_id LIKE ? OR s.legacy_item_id LIKE ?)")
            ep += [f"%{q}%"] * 3
        if request.args.get("channel"):
            extra.append("COALESCE(s.channel, 'eBay') = ?")
            ep.append(request.args["channel"])
        where, params = sales_where(start, end, " AND ".join(extra))
        conn = connect()
        try:
            sales = profit.compute_sales(conn, where, tuple(params + ep))
        finally:
            conn.close()
        if request.args.get("missing_cost") in ("1", "true"):
            sales = [s for s in sales if s["item_cost"] is None and not s["in_bulk"]]
        limit = min(parse_int(request.args.get("limit")) or 200, 2000)
        return jsonify({"count": len(sales), "sales": [_sale_json(s) for s in sales[:limit]]})

    @app.route(PREFIX + "/sales/<line_id>", methods=["GET"])
    @api_auth
    def api_v1_sale(line_id):
        conn = connect()
        try:
            got = profit.compute_sales(conn, "s.line_item_id = ?", (line_id,))
        finally:
            conn.close()
        if not got:
            raise ApiError("No sale with that line_item_id.", 404)
        return jsonify(_sale_json(got[0]))

    @app.route(PREFIX + "/sales/<line_id>", methods=["PATCH"])
    @api_auth
    def api_v1_sale_update(line_id):
        b = _body()
        allowed = {"cost", "postage_cost", "packaging_cost", "handling_cost", "other_cost"}
        conn = connect()
        try:
            row = conn.execute("SELECT * FROM sales WHERE line_item_id=?", (line_id,)).fetchone()
            if not row:
                raise ApiError("No sale with that line_item_id.", 404)
            _guard_closed([row["sold_at"]], b)
            sets, vals = [], []
            for k in allowed & b.keys():
                sets.append(f"{k}=?")
                vals.append(_money(b[k], k))
            if "bulk_buy_id" in b:
                bid = parse_int(b["bulk_buy_id"]) if b["bulk_buy_id"] not in (None, "") else None
                if bid and not conn.execute("SELECT 1 FROM bulk_buys WHERE id=?", (bid,)).fetchone():
                    raise ApiError("No bulk buy with that id.", 404)
                sets.append("bulk_buy_id=?")
                vals.append(bid)
            if "notes" in b:
                sets.append("notes=?")
                vals.append(b["notes"] or None)
            if not sets:
                raise ApiError("Nothing to change. Send one of: " + ", ".join(sorted(allowed | {"bulk_buy_id", "notes"})))
            with write_lock():
                conn.execute(f"UPDATE sales SET {', '.join(sets)}, updated_at=? WHERE line_item_id=?",
                             vals + [now_iso(), line_id])
                conn.commit()
            s = profit.compute_sales(conn, "s.line_item_id = ?", (line_id,))[0]
        finally:
            conn.close()
        return jsonify(_sale_json(s))

    @app.route(PREFIX + "/sales", methods=["POST"])
    @api_auth
    def api_v1_sale_create():
        b = _body()
        if not b.get("title") or b.get("item_price") in (None, ""):
            raise ApiError("'title' and 'item_price' are required.")
        sold = _date(b.get("sold_at"), "sold_at", date.today().isoformat())
        _guard_closed([sold], b)
        item_id = parse_int(b.get("item_id"))
        conn = connect()
        try:
            bulk = None
            if item_id:
                r = conn.execute("SELECT bulk_buy_id FROM items WHERE id=?", (item_id,)).fetchone()
                if not r:
                    raise ApiError("No item with that item_id.", 404)
                bulk = r["bulk_buy_id"]
            lid = "M-" + uuid.uuid4().hex[:12]
            with write_lock():
                conn.execute(
                    """INSERT INTO sales(line_item_id, order_id, title, quantity, item_price, delivery_charged,
                           manual_fees, postage_cost, packaging_cost, handling_cost, cost, channel, sold_at, item_id,
                           bulk_buy_id, notes, source, currency, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'manual', 'GBP', ?)""",
                    (lid, lid, b["title"], max(parse_int(b.get("quantity")) or 1, 1), _money(b["item_price"], "item_price"),
                     _money(b.get("delivery_charged"), "delivery_charged") or 0, _money(b.get("fees"), "fees") or 0,
                     _money(b.get("postage_cost"), "postage_cost"), _money(b.get("packaging_cost"), "packaging_cost"),
                     _money(b.get("handling_cost"), "handling_cost"), _money(b.get("cost"), "cost"),
                     b.get("channel") or "Other", sold + "T12:00:00Z", item_id, bulk, b.get("notes"), now_iso()))
                if item_id:
                    add_event(conn, item_id, "sold", f"Sold on {b.get('channel') or 'Other'} for "
                              f"{fmt_money(_money(b['item_price'], 'item_price'))}", at=sold)
                conn.commit()
            s = profit.compute_sales(conn, "s.line_item_id = ?", (lid,))[0]
            ebay_note = None
            if item_id and str(b.get("update_ebay", "")).lower() in ("1", "true", "yes"):
                import stores
                ebay_note = stores.propagate_sale(conn, lid, item_id, max(parse_int(b.get("quantity")) or 1, 1),
                                                  b.get("channel") or "Other") or "No live listings for this item."
        finally:
            conn.close()
        out = _sale_json(s)
        if ebay_note:
            out["ebay_update"] = ebay_note
        return jsonify(out), 201

    # ---------------- items ----------------
    def _load(conn, where="i.archived = 0", params=()):
        from features import load_items
        return load_items(conn, where, params)

    @app.route(PREFIX + "/items", methods=["GET"])
    @api_auth
    def api_v1_items():
        status = request.args.get("status", "instock")
        where, params = ["i.archived = 0"], []
        for f in ("location", "source"):
            if request.args.get(f):
                where.append(f"i.{f} = ?")
                params.append(request.args[f])
        if request.args.get("q"):
            where.append("(i.title LIKE ? OR i.sku LIKE ?)")
            params += [f"%{request.args['q']}%"] * 2
        conn = connect()
        try:
            items = _load(conn, " AND ".join(where), tuple(params))
        finally:
            conn.close()
        if status != "all":
            items = [i for i in items if (i["status"] in ("listed", "unlisted") if status == "instock"
                                          else i["status"] == status)]
        if request.args.get("missing_cost") in ("1", "true"):
            items = [i for i in items if i["unit_cost"] is None]
        return jsonify({"count": len(items), "items": [_item_json(i) for i in items]})

    @app.route(PREFIX + "/items/<int:item_id>", methods=["GET"])
    @api_auth
    def api_v1_item(item_id):
        conn = connect()
        try:
            got = _load(conn, "i.id = ?", (item_id,))
            if not got:
                raise ApiError("No item with that id.", 404)
            out = _item_json(got[0])
            out["listings"] = [dict(r) for r in conn.execute(
                "SELECT legacy_item_id, title, price, status, start_time, watch_count, promoted FROM listings "
                "WHERE item_id=? ORDER BY start_time DESC", (item_id,))]
            out["sales"] = [_sale_json(s) for s in profit.compute_sales(conn, "s.item_id = ?", (item_id,))]
            out["timeline"] = [dict(r) for r in conn.execute(
                "SELECT at, kind, detail FROM item_events WHERE item_id=? ORDER BY at DESC LIMIT 50", (item_id,))]
        finally:
            conn.close()
        return jsonify(out)

    ITEM_FIELDS = {"title": str, "sku": str, "quantity": int, "cost": "money", "cost_locked": bool, "source": str,
                   "purchased_at": "date", "location": str, "bulk_buy_id": int, "notes": str,
                   "packaging_cost": "money", "handling_cost": "money", "restock_url": "url"}

    def _item_values(b, partial):
        from features import _clean_url
        out = {}
        for k, t in ITEM_FIELDS.items():
            if k not in b:
                continue
            v = b[k]
            if t == "money":
                v = _money(v, k)
            elif t == "date":
                v = _date(v, k)
            elif t is int:
                v = parse_int(v) if v not in (None, "") else None
            elif t is bool:
                v = 1 if v in (True, 1, "1", "true", "yes") else 0
            elif t == "url":
                v = _clean_url(v)
            else:
                v = (str(v).strip() or None) if v is not None else None
            out[k] = v
        if not partial:
            out.setdefault("quantity", 1)
            out["quantity"] = max(out["quantity"] or 1, 1)
        return out

    @app.route(PREFIX + "/items", methods=["POST"])
    @api_auth
    def api_v1_item_create():
        b = _body()
        titles = [t for t in (b.get("titles") or [b.get("title")]) if t and str(t).strip()]
        if not titles:
            raise ApiError("'title' (or 'titles': [...]) is required.")
        vals = _item_values(b, partial=False)
        _guard_closed([vals.get("purchased_at")], b)
        ids = []
        conn = connect()
        try:
            if vals.get("bulk_buy_id") and not conn.execute("SELECT 1 FROM bulk_buys WHERE id=?",
                                                            (vals["bulk_buy_id"],)).fetchone():
                raise ApiError("No bulk buy with that id.", 404)
            with write_lock():
                for t in titles:
                    v = dict(vals, title=str(t).strip())
                    cols = list(v) + ["created_at", "updated_at"]
                    cur = conn.execute(f"INSERT INTO items({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                       list(v.values()) + [now_iso(), now_iso()])
                    ids.append(cur.lastrowid)
                    add_event(conn, cur.lastrowid, "created", "Added by API" +
                              (f" · bought from {v['source']}" if v.get("source") else "") +
                              (f" for {fmt_money(v['cost'])}" if v.get("cost") is not None else ""),
                              at=v.get("purchased_at"))
                conn.commit()
            items = _load(conn, f"i.id IN ({','.join('?' * len(ids))})", tuple(ids))
        finally:
            conn.close()
        return jsonify({"created": len(ids), "items": [_item_json(i) for i in items]}), 201

    @app.route(PREFIX + "/items/<int:item_id>", methods=["PATCH"])
    @api_auth
    def api_v1_item_update(item_id):
        from features import _sync_item_sales
        b = _body()
        vals = _item_values(b, partial=True)
        conn = connect()
        try:
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if not row:
                raise ApiError("No item with that id.", 404)
            dates = [row["purchased_at"], vals.get("purchased_at")] + [
                r[0] for r in conn.execute("SELECT sold_at FROM sales WHERE item_id=?", (item_id,))]
            _guard_closed(dates, b)
            with write_lock():
                if vals:
                    conn.execute(f"UPDATE items SET {', '.join(k + '=?' for k in vals)}, updated_at=? WHERE id=?",
                                 list(vals.values()) + [now_iso(), item_id])
                    changed = ", ".join(f"{k}={v}" for k, v in vals.items())
                    add_event(conn, item_id, "edited", f"Updated by API: {changed}"[:300])
                if "bulk_buy_id" in vals:
                    _sync_item_sales(conn, item_id)
                link = str(b.get("link_listing") or "").strip()
                if link:
                    if not conn.execute("SELECT 1 FROM listings WHERE legacy_item_id=?", (link,)).fetchone():
                        raise ApiError("No synced listing with that eBay item ID.", 404)
                    conn.execute("UPDATE listings SET item_id=? WHERE legacy_item_id=?", (item_id, link))
                    conn.execute("UPDATE sales SET item_id=? WHERE legacy_item_id=?", (item_id, link))
                    _sync_item_sales(conn, item_id)
                    add_event(conn, item_id, "linked", f"Linked to listing #{link}", link)
                if not vals and not link:
                    raise ApiError("Nothing to change. Fields: " + ", ".join(ITEM_FIELDS) + ", link_listing")
                conn.commit()
            out = _item_json(_load(conn, "i.id = ?", (item_id,))[0])
        finally:
            conn.close()
        return jsonify(out)

    @app.route(PREFIX + "/items/<int:item_id>/write-off", methods=["POST"])
    @api_auth
    def api_v1_item_writeoff(item_id):
        b = _body()
        d = _date(b.get("date"), "date", date.today().isoformat())
        _guard_closed([d], b)
        conn = connect()
        try:
            if not conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone():
                raise ApiError("No item with that id.", 404)
            with write_lock():
                conn.execute("UPDATE items SET written_off_at=?, write_off_reason=? WHERE id=?",
                             (d, (b.get("reason") or "Written off")[:80], item_id))
                add_event(conn, item_id, "written_off", f"Written off: {b.get('reason') or 'no reason given'}", at=d)
                conn.commit()
        finally:
            conn.close()
        return jsonify({"ok": True, "item_id": item_id, "written_off_at": d})

    # ---------------- bulk buys ----------------
    @app.route(PREFIX + "/bulk-buys", methods=["GET"])
    @api_auth
    def api_v1_bulks():
        conn = connect()
        try:
            out = []
            for b in conn.execute("SELECT * FROM bulk_buys ORDER BY purchased_at DESC, id DESC"):
                b = dict(b)
                summ = profit.bulk_summary(b, profit.compute_sales(conn, "s.bulk_buy_id = ?", (b["id"],)))
                out.append({**{k: b[k] for k in ("id", "name", "purchased_at", "total_cost", "extra_costs",
                                                 "item_count", "source", "location", "notes")},
                            "split_method": b.get("split_method"), "cost": summ["cost"],
                            "money_made": summ["money_made"], "profit": summ["profit"], "roi_pct": summ["roi"],
                            "paid_back_pct": summ["recovered_pct"], "units_sold": summ["units"],
                            "items_left": summ["items_left"]})
        finally:
            conn.close()
        return jsonify({"count": len(out), "bulk_buys": out})

    BULK_FIELDS = {"name": str, "purchased_at": "date", "total_cost": "money", "extra_costs": "money",
                   "item_count": int, "source": str, "location": str, "notes": str, "split_method": str}

    def _bulk_values(b):
        out = {}
        for k, t in BULK_FIELDS.items():
            if k in b:
                v = b[k]
                out[k] = (_money(v, k) if t == "money" else _date(v, k) if t == "date"
                          else (parse_int(v) if v not in (None, "") else None) if t is int
                          else (str(v).strip() or None) if v is not None else None)
        if out.get("split_method") not in (None, "even", "price"):
            raise ApiError("'split_method' must be 'even' or 'price'.")
        return out

    @app.route(PREFIX + "/bulk-buys", methods=["POST"])
    @api_auth
    def api_v1_bulk_create():
        b = _body()
        vals = _bulk_values(b)
        if not vals.get("name") or vals.get("total_cost") is None:
            raise ApiError("'name' and 'total_cost' are required.")
        vals.setdefault("purchased_at", date.today().isoformat())
        _guard_closed([vals["purchased_at"]], b)
        conn = connect()
        try:
            with write_lock():
                cols = list(vals) + ["created_at"]
                bid = conn.execute(f"INSERT INTO bulk_buys({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                   list(vals.values()) + [now_iso()]).lastrowid
                for t in [t for t in (b.get("items") or []) if t and str(t).strip()]:
                    iid = conn.execute("""INSERT INTO items(title, quantity, bulk_buy_id, source, location, purchased_at,
                                          created_at, updated_at) VALUES (?,1,?,?,?,?,?,?)""",
                                       (str(t).strip(), bid, vals.get("source"), vals.get("location"),
                                        vals["purchased_at"], now_iso(), now_iso())).lastrowid
                    add_event(conn, iid, "created", f"Part of bulk buy “{vals['name']}” (added by API)",
                              at=vals["purchased_at"])
                conn.commit()
        finally:
            conn.close()
        return jsonify({"id": bid, **vals, "items_logged": len(b.get("items") or [])}), 201

    @app.route(PREFIX + "/bulk-buys/<int:bulk_id>", methods=["PATCH"])
    @api_auth
    def api_v1_bulk_update(bulk_id):
        b = _body()
        vals = _bulk_values(b)
        if not vals:
            raise ApiError("Nothing to change. Fields: " + ", ".join(BULK_FIELDS))
        conn = connect()
        try:
            row = conn.execute("SELECT * FROM bulk_buys WHERE id=?", (bulk_id,)).fetchone()
            if not row:
                raise ApiError("No bulk buy with that id.", 404)
            _guard_closed([row["purchased_at"], vals.get("purchased_at")] + [
                r[0] for r in conn.execute("SELECT sold_at FROM sales WHERE bulk_buy_id=?", (bulk_id,))], b)
            with write_lock():
                conn.execute(f"UPDATE bulk_buys SET {', '.join(k + '=?' for k in vals)} WHERE id=?",
                             list(vals.values()) + [bulk_id])
                conn.commit()
        finally:
            conn.close()
        return jsonify({"ok": True, "id": bulk_id, **vals})

    @app.route(PREFIX + "/bulk-buys/<int:bulk_id>/link-listings", methods=["POST"])
    @api_auth
    def api_v1_bulk_link(bulk_id):
        """Same as the bulk buy page's 'Link existing eBay listings'."""
        b = _body()
        ids = [str(i).strip() for i in (b.get("listing_ids") or []) if str(i).strip()]
        if not ids:
            raise ApiError("'listing_ids' must be a list of eBay item IDs.")
        conn = connect()
        try:
            bb = conn.execute("SELECT * FROM bulk_buys WHERE id=?", (bulk_id,)).fetchone()
            if not bb:
                raise ApiError("No bulk buy with that id.", 404)
            ph = ",".join("?" * len(ids))
            _guard_closed([bb["purchased_at"]] + [r[0] for r in conn.execute(
                f"SELECT sold_at FROM sales WHERE legacy_item_id IN ({ph})", ids)], b)
            linked, unknown = [], []
            with write_lock():
                for lid in ids:
                    L = conn.execute("SELECT * FROM listings WHERE legacy_item_id=?", (lid,)).fetchone()
                    item_id = L["item_id"] if L else None
                    if item_id is None:
                        sold = conn.execute("SELECT item_id, title, SUM(quantity) q FROM sales WHERE legacy_item_id=? "
                                            "GROUP BY legacy_item_id", (lid,)).fetchone()
                        if not L and not sold:
                            unknown.append(lid)
                            continue
                        item_id = sold["item_id"] if sold and sold["item_id"] else conn.execute(
                            "INSERT INTO items(title, quantity, created_at, updated_at) VALUES (?,?,?,?)",
                            ((L["title"] if L else sold["title"]), (L["quantity"] if L else sold["q"]) or 1,
                             now_iso(), now_iso())).lastrowid
                        if L:
                            conn.execute("UPDATE listings SET item_id=? WHERE legacy_item_id=?", (item_id, lid))
                    conn.execute("""UPDATE items SET bulk_buy_id=?, source=COALESCE(source, ?), location=COALESCE(location, ?),
                                    purchased_at=COALESCE(purchased_at, ?), updated_at=? WHERE id=?""",
                                 (bulk_id, bb["source"], bb["location"], bb["purchased_at"], now_iso(), item_id))
                    conn.execute("UPDATE sales SET item_id=?, bulk_buy_id=? WHERE legacy_item_id=? OR item_id=?",
                                 (item_id, bulk_id, lid, item_id))
                    conn.execute("""INSERT INTO listing_rules(legacy_item_id, bulk_buy_id, unit_cost) VALUES (?,?,NULL)
                                    ON CONFLICT(legacy_item_id) DO UPDATE SET bulk_buy_id=excluded.bulk_buy_id""",
                                 (lid, bulk_id))
                    add_event(conn, item_id, "linked", f"Added to bulk buy “{bb['name']}” by API", lid)
                    linked.append(lid)
                conn.commit()
        finally:
            conn.close()
        return jsonify({"linked": linked, "not_found": unknown})

    # ---------------- listings ----------------
    @app.route(PREFIX + "/listings")
    @api_auth
    def api_v1_listings():
        status = request.args.get("status", "active")
        where, params = [], []
        if status != "all":
            where.append("status = ?")
            params.append(status)
        if request.args.get("q"):
            where.append("title LIKE ?")
            params.append(f"%{request.args['q']}%")
        conn = connect()
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT legacy_item_id, item_id, title, sku, price, original_price, quantity_available, quantity_sold, "
                "status, start_time, watch_count, promoted, relist_count, url FROM listings" +
                (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY start_time DESC LIMIT 1000", params)]
        finally:
            conn.close()
        return jsonify({"count": len(rows), "listings": rows})

    # ---------------- expenses & mileage ----------------
    @app.route(PREFIX + "/expenses", methods=["GET"])
    @api_auth
    def api_v1_expenses():
        from common import tax_year_range
        y = parse_int(request.args.get("year")) or tax_year_of(date.today())
        s, e = tax_year_range(y)
        conn = connect()
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT id, date, category, description, amount, item_id, receipt_path IS NOT NULL AS has_receipt "
                "FROM expenses WHERE date >= ? AND date < ? ORDER BY date DESC", (s, e))]
        finally:
            conn.close()
        return jsonify({"tax_year": tax_year_label(y), "count": len(rows), "total": round(sum(r["amount"] or 0 for r in rows), 2),
                        "expenses": rows})

    @app.route(PREFIX + "/expenses", methods=["POST"])
    @api_auth
    def api_v1_expense_create():
        b = _body()
        cat = b.get("category")
        if cat not in taxcalc.CATEGORY_BOX:
            raise ApiError("'category' must be one of: " + ", ".join(taxcalc.CATEGORY_BOX))
        if not b.get("description") or b.get("amount") in (None, ""):
            raise ApiError("'description' and 'amount' are required.")
        d = _date(b.get("date"), "date", date.today().isoformat())
        _guard_closed([d], b)
        conn = connect()
        try:
            with write_lock():
                eid = conn.execute("INSERT INTO expenses(date, category, description, amount, item_id, created_at) "
                                   "VALUES (?,?,?,?,?,?)", (d, cat, str(b["description"])[:200], _money(b["amount"], "amount"),
                                                            parse_int(b.get("item_id")), now_iso())).lastrowid
                conn.commit()
        finally:
            conn.close()
        return jsonify({"id": eid, "date": d, "category": cat, "sa103s_box": taxcalc.CATEGORY_BOX[cat],
                        "description": b["description"], "amount": _money(b["amount"], "amount")}), 201

    @app.route(PREFIX + "/mileage", methods=["POST"])
    @api_auth
    def api_v1_mileage_create():
        b = _body()
        miles = parse_money(b.get("miles"))
        if not miles or not b.get("purpose"):
            raise ApiError("'miles' and 'purpose' are required.")
        if b.get("return_trip") in (True, 1, "1", "true", "yes"):
            miles *= 2
        vehicle = b.get("vehicle") or "car"
        if vehicle not in ("car", "van", "motorcycle", "bicycle"):
            raise ApiError("'vehicle' must be car, van, motorcycle or bicycle.")
        d = _date(b.get("date"), "date", date.today().isoformat())
        _guard_closed([d], b)
        conn = connect()
        try:
            with write_lock():
                mid = conn.execute("INSERT INTO mileage(date, miles, vehicle, purpose, route, created_at) VALUES (?,?,?,?,?,?)",
                                   (d, miles, vehicle, str(b["purpose"])[:200], (b.get("route") or "")[:200], now_iso())).lastrowid
                conn.commit()
        finally:
            conn.close()
        hi, lo = taxcalc.mileage_rates(vehicle, tax_year_of(d))
        return jsonify({"id": mid, "date": d, "miles": miles, "vehicle": vehicle,
                        "claim_at_first_10000_rate": round(miles * hi, 2)}), 201

    # ---------------- tax ----------------
    @app.route(PREFIX + "/tax")
    @api_auth
    def api_v1_tax():
        y = parse_int(request.args.get("year")) or tax_year_of(date.today())
        basis = request.args.get("basis") or Settings().get("tax_basis") or "cash"
        conn = connect()
        try:
            r = taxcalc.compute(conn, y, basis)
        finally:
            conn.close()
        r.pop("sales", None)
        r["box_labels"] = {str(k): v for k, v in taxcalc.BOX_LABELS.items()}
        return jsonify(r)

    # ---------------- notifications & sync ----------------
    @app.route(PREFIX + "/notifications")
    @api_auth
    def api_v1_notifications():
        where, params = [], []
        if request.args.get("unread") in ("1", "true"):
            where.append("read = 0")
        if request.args.get("kind"):
            where.append("kind = ?")
            params.append(request.args["kind"])
        conn = connect()
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT id, at, kind, title, body, link, read FROM notifications" +
                (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY at DESC, id DESC LIMIT 100", params)]
        finally:
            conn.close()
        return jsonify({"count": len(rows), "notifications": rows})

    @app.route(PREFIX + "/sync", methods=["POST"])
    @api_auth
    def api_v1_sync():
        import sync
        from flask import current_app
        if current_app.config.get("DEMO") or db.database_override():
            return jsonify({"started": False, "message": "Sync is disabled in demo mode."})
        started = sync.start_background(full=bool(_body().get("full")))
        return jsonify({"started": started, "message": "Sync started." if started else "A sync is already running."})
