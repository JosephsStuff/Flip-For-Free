"""Inventory, multi-marketplace sales, import, insights, automations, UK tax and notifications."""
import csv
import io
import json
import threading
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from flask import Response, abort, flash, jsonify, redirect, render_template, request, url_for

import automation
import profit
import taxcalc
from common import (PERIODS, closed_guard, closed_years, login_required, parse_float, parse_int, parse_money,
                    period_range, sales_where, tax_year_label, tax_year_of)
from db import Settings, add_event, connect, now_iso, write_lock

CHANNELS = ["eBay", "Vinted", "CardTrader", "Shopify", "Facebook Marketplace", "Depop", "Etsy", "Amazon", "Gumtree",
            "Market stall",
            "Car boot", "In person", "Other"]


def _parse(ts):
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _days(a, b):
    a, b = _parse(a), _parse(b)
    if not a or not b:
        return None
    return (b - a).total_seconds() / 86400


# --------------------------------------------------------------------------- #
# Inventory helpers
# --------------------------------------------------------------------------- #
def load_items(conn, where="1=1", params=()):
    unit = profit.item_unit_costs(conn)
    rows = [dict(r) for r in conn.execute(f"""
        SELECT i.*, b.name AS bulk_name,
          (SELECT COALESCE(SUM(quantity), 0) FROM sales s WHERE s.item_id = i.id
              AND COALESCE(s.status, '') <> 'cancelled') AS sold_units,
          (SELECT COUNT(*) FROM listings l WHERE l.item_id = i.id AND l.status = 'active') AS active_listings,
          (SELECT price FROM listings l WHERE l.item_id = i.id AND l.status = 'active'
              ORDER BY start_time DESC LIMIT 1) AS list_price,
          (SELECT legacy_item_id FROM listings l WHERE l.item_id = i.id AND l.status = 'active'
              ORDER BY start_time DESC LIMIT 1) AS active_id,
          (SELECT MIN(start_time) FROM listings l WHERE l.item_id = i.id) AS first_listed,
          (SELECT MAX(promoted) FROM listings l WHERE l.item_id = i.id AND l.status = 'active') AS promoted,
          (SELECT MAX(sold_at) FROM sales s WHERE s.item_id = i.id) AS last_sold,
          (SELECT image_url FROM listings l WHERE l.item_id = i.id AND image_url IS NOT NULL
              ORDER BY start_time DESC LIMIT 1) AS image_url
        FROM items i LEFT JOIN bulk_buys b ON b.id = i.bulk_buy_id
        WHERE {where} ORDER BY COALESCE(i.purchased_at, i.created_at) DESC, i.id DESC""", params)]
    for it in rows:
        it["unit_cost"] = unit.get(it["id"])
        qty = it["quantity"] or 1
        if it.get("written_off_at"):
            it["status"] = "written off"
        elif it["sold_units"] >= qty:
            it["status"] = "sold"
        elif it["active_listings"]:
            it["status"] = "listed"
        else:
            it["status"] = "unlisted"
        it["remaining"] = max(qty - it["sold_units"], 0)
        it["days_listed"] = int(_days(it["first_listed"], now_iso()) or 0) if it["first_listed"] else None
    return rows


def _item_form():
    f = request.form
    return {
        "title": (f.get("title") or "").strip() or "Untitled item",
        "sku": (f.get("sku") or "").strip() or None,
        "quantity": max(parse_int(f.get("quantity")) or 1, 1),
        "cost": parse_money(f.get("cost")),
        "cost_locked": 1 if f.get("cost_locked") else 0,
        "source": (f.get("source") or "").strip() or None,
        "purchased_at": f.get("purchased_at") or None,
        "location": (f.get("location") or "").strip() or None,
        "bulk_buy_id": parse_int(f.get("bulk_buy_id")),
        "notes": (f.get("notes") or "").strip() or None,
        "restock_url": _clean_url(f.get("restock_url")),
        "packaging_cost": parse_money(f.get("packaging_cost")),
        "handling_cost": parse_money(f.get("handling_cost")),
        "relist_exclude": 1 if f.get("relist_exclude") else 0,
        "relist_floor": parse_money(f.get("relist_floor")),
    }


def _clean_url(u):
    """Only keep web links (stops javascript: and other odd schemes being saved as a 'link')."""
    import re as _re
    u = (u or "").strip()
    if not u:
        return None
    if _re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", u) and not u.lower().startswith(("http://", "https://")):
        return None          # javascript:, data:, file: ... are not links we store
    if not u.lower().startswith(("http://", "https://")):
        u = "https://" + u
    return u[:1000]


def _distinct(conn, col):
    return [r[0] for r in conn.execute(
        f"SELECT DISTINCT {col} FROM items WHERE {col} IS NOT NULL AND {col} <> '' ORDER BY {col}")]


def _sync_item_sales(conn, item_id):
    """Keep sales of an item pointing at the item's bulk buy."""
    it = conn.execute("SELECT bulk_buy_id FROM items WHERE id=?", (item_id,)).fetchone()
    if it is not None:
        conn.execute("UPDATE sales SET bulk_buy_id=? WHERE item_id=?", (it["bulk_buy_id"], item_id))


def register(app):  # noqa: C901  (one place to see every route)

    # ======================================================================= #
    # Inventory
    # ======================================================================= #
    @app.route("/inventory")
    @login_required
    def inventory():
        status = request.args.get("status", "instock")
        q = (request.args.get("q") or "").strip()
        loc = request.args.get("location", "")
        src = request.args.get("source", "")
        flt = request.args.get("filter", "")
        where, params = ["i.archived = 0"], []
        if q:
            where.append("(i.title LIKE ? OR i.sku LIKE ? OR i.notes LIKE ?)")
            params += [f"%{q}%"] * 3
        if loc:
            where.append("i.location = ?")
            params.append(loc)
        if src:
            where.append("i.source = ?")
            params.append(src)
        if flt == "no_date":
            where.append("i.purchased_at IS NULL AND i.cost IS NOT NULL")
        if flt == "no_cost":
            where.append("i.cost IS NULL AND i.bulk_buy_id IS NULL")
        conn = connect()
        try:
            items = load_items(conn, " AND ".join(where), tuple(params))
            bulks = [dict(r) for r in conn.execute("SELECT id, name FROM bulk_buys ORDER BY name")]
            locations, sources = _distinct(conn, "location"), _distinct(conn, "source")
        finally:
            conn.close()
        counts = defaultdict(int)
        for it in items:
            counts[it["status"]] += 1
        counts["instock"] = counts["listed"] + counts["unlisted"]
        counts["all"] = len(items)
        if status != "all":
            items = [i for i in items if (i["status"] in ("listed", "unlisted") if status == "instock"
                                          else i["status"] == status)]
        stock_value = sum((i["unit_cost"] or 0) * i["remaining"] for i in items if i["status"] in ("listed", "unlisted"))
        list_value = sum((i["list_price"] or 0) * i["remaining"] for i in items if i["status"] == "listed")
        return render_template("inventory.html", items=items, status=status, counts=counts, q=q, bulks=bulks,
                               locations=locations, sources=sources, loc=loc, src=src, filter=flt,
                               stock_value=stock_value, list_value=list_value, today=date.today().isoformat())

    @app.route("/inventory/new", methods=["POST"])
    @login_required
    def inventory_new():
        f = _item_form()
        lines = [l.strip() for l in (request.form.get("titles") or "").splitlines() if l.strip()]
        titles = lines or [f["title"]]
        guard = closed_guard([f["purchased_at"]], "Adding stock")
        if guard:
            return guard
        conn = connect()
        try:
            with write_lock():
                for t in titles:
                    cur = conn.execute(
                        """INSERT INTO items(title, sku, quantity, cost, cost_locked, source, purchased_at, location,
                               bulk_buy_id, notes, restock_url, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (t, f["sku"] if len(titles) == 1 else None, f["quantity"], f["cost"], f["cost_locked"],
                         f["source"], f["purchased_at"], f["location"], f["bulk_buy_id"], f["notes"],
                         f["restock_url"], now_iso(), now_iso()))
                    add_event(conn, cur.lastrowid, "created",
                              "Bought" + (f" from {f['source']}" if f["source"] else "")
                              + (f" for £{f['cost']:,.2f}" if f["cost"] is not None else ""), at=f["purchased_at"])
                conn.commit()
        finally:
            conn.close()
        flash(f"Added {len(titles)} item{'s' if len(titles) != 1 else ''} to inventory.", "ok")
        return redirect(request.form.get("back") or url_for("inventory", status="unlisted"))

    @app.route("/inventory/<int:item_id>", methods=["GET", "POST"])
    @login_required
    def item_detail(item_id):
        conn = connect()
        try:
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if not row:
                abort(404)
            if request.method == "POST":
                f = _item_form()
                dates = [row["purchased_at"], f["purchased_at"]] + [
                    r[0] for r in conn.execute("SELECT sold_at FROM sales WHERE item_id=?", (item_id,))]
                if row["bulk_buy_id"] or f["bulk_buy_id"]:
                    dates += [r[0] for r in conn.execute(
                        "SELECT sold_at FROM sales WHERE bulk_buy_id IN (?, ?)",
                        (row["bulk_buy_id"] or -1, f["bulk_buy_id"] or -1))]
                guard = closed_guard(dates, "Changing this item")
                if guard:
                    return guard
                with write_lock():
                    conn.execute("""UPDATE items SET title=?, sku=?, quantity=?, cost=?, cost_locked=?, source=?,
                                    purchased_at=?, location=?, bulk_buy_id=?, notes=?, relist_exclude=?, relist_floor=?,
                                    restock_url=?, packaging_cost=?, handling_cost=?, updated_at=? WHERE id=?""",
                                 (f["title"], f["sku"], f["quantity"], f["cost"], f["cost_locked"], f["source"],
                                  f["purchased_at"], f["location"], f["bulk_buy_id"], f["notes"], f["relist_exclude"],
                                  f["relist_floor"], f["restock_url"], f["packaging_cost"], f["handling_cost"],
                                  now_iso(), item_id))
                    if (row["cost"] or None) != f["cost"]:
                        add_event(conn, item_id, "cost", "Cost set to " +
                                  ("—" if f["cost"] is None else f"£{f['cost']:,.2f}"))
                    if (row["location"] or None) != f["location"]:
                        add_event(conn, item_id, "location", f"Moved to {f['location'] or '—'}")
                    if row["bulk_buy_id"] != f["bulk_buy_id"]:
                        _sync_item_sales(conn, item_id)
                    link = (request.form.get("link_listing") or "").strip()
                    if link:
                        conn.execute("UPDATE listings SET item_id=? WHERE legacy_item_id=?", (item_id, link))
                        conn.execute("UPDATE sales SET item_id=? WHERE legacy_item_id=?", (item_id, link))
                        _sync_item_sales(conn, item_id)
                        add_event(conn, item_id, "linked", f"Linked to listing #{link}", link)
                    conn.commit()
                flash("Item saved.", "ok")
                return redirect(url_for("item_detail", item_id=item_id))
            it = load_items(conn, "i.id = ?", (item_id,))[0]
            listings = [dict(r) for r in conn.execute(
                "SELECT * FROM listings WHERE item_id=? ORDER BY start_time DESC", (item_id,))]
            sales = profit.compute_sales(conn, "s.item_id = ?", (item_id,))
            events = [dict(r) for r in conn.execute(
                "SELECT * FROM item_events WHERE item_id=? ORDER BY at DESC, id DESC", (item_id,))]
            bulks = [dict(r) for r in conn.execute("SELECT id, name FROM bulk_buys ORDER BY name")]
            unlinked = [dict(r) for r in conn.execute(
                """SELECT legacy_item_id, title, price FROM listings WHERE status='active'
                   AND (item_id IS NULL OR item_id NOT IN (SELECT id FROM items WHERE archived=0))
                   ORDER BY title LIMIT 300""")]
            alloc = profit.bulk_allocations(conn).get(it["bulk_buy_id"]) if it["bulk_buy_id"] else None
            locations, sources = _distinct(conn, "location"), _distinct(conn, "source")
        finally:
            conn.close()
        defaults = {"packaging": None, "handling": None}
        c2 = connect()
        try:
            defaults = profit.packing_defaults(c2)
        finally:
            c2.close()
        import timetosell
        conn = connect()
        try:
            tts = timetosell.for_item(conn, item_id)
        finally:
            conn.close()
        return render_template("item.html", tts=tts, it=it, listings=listings, sales=sales, events=events, bulks=bulks,
                               defaults=defaults,
                               unlinked=unlinked, alloc=alloc, locations=locations, sources=sources,
                               today=date.today().isoformat())

    @app.route("/inventory/<int:item_id>/write-off", methods=["POST"])
    @login_required
    def item_write_off(item_id):
        undo = request.form.get("undo") == "1"
        d = request.form.get("date") or date.today().isoformat()
        conn = connect()
        try:
            row = conn.execute("SELECT written_off_at FROM items WHERE id=?", (item_id,)).fetchone()
            if not row:
                abort(404)
            guard = closed_guard([d, row["written_off_at"]], "This write-off")
            if guard:
                return guard
            with write_lock():
                if undo:
                    conn.execute("UPDATE items SET written_off_at=NULL, write_off_reason=NULL WHERE id=?", (item_id,))
                    add_event(conn, item_id, "location", "Write-off undone")
                else:
                    reason = request.form.get("reason") or "Other"
                    conn.execute("UPDATE items SET written_off_at=?, write_off_reason=? WHERE id=?",
                                 (d, reason, item_id))
                    add_event(conn, item_id, "error", f"Written off: {reason}", at=d)
                conn.commit()
        finally:
            conn.close()
        flash("Write-off undone." if undo else "Item written off. Its cost now shows as a write-off on your P&L.", "ok")
        return redirect(url_for("item_detail", item_id=item_id))

    @app.route("/inventory/<int:item_id>/restock", methods=["POST"])
    @login_required
    def item_restock(item_id):
        """You bought more of this: add a new stock entry with the same details (new cost and date)."""
        conn = connect()
        try:
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if not row:
                abort(404)
            qty = max(parse_int(request.form.get("quantity")) or 1, 1)
            cost = parse_money(request.form.get("cost"))
            bought = request.form.get("purchased_at") or date.today().isoformat()
            guard = closed_guard([bought], "Adding this restock")
            if guard:
                return guard
            with write_lock():
                cur = conn.execute(
                    """INSERT INTO items(title, sku, quantity, cost, source, purchased_at, location, notes, restock_url,
                           created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (row["title"], row["sku"], qty, cost, request.form.get("source") or row["source"], bought,
                     row["location"], f"Restock of item #{item_id}", row["restock_url"], now_iso(), now_iso()))
                new_id = cur.lastrowid
                msg = f"Restocked {qty}" + (f" at £{cost:,.2f} each" if cost is not None else "")
                add_event(conn, item_id, "restocked", f"{msg} (new stock entry #{new_id})")
                add_event(conn, new_id, "created", f"{msg} from {request.form.get('source') or row['source'] or '—'}",
                          at=bought)
                conn.commit()
        finally:
            conn.close()
        flash("Restock added to inventory. List it on eBay and it will link up by SKU or title.", "ok")
        return redirect(url_for("item_detail", item_id=new_id))

    @app.route("/inventory/<int:item_id>/archive", methods=["POST"])
    @login_required
    def item_archive(item_id):
        conn = connect()
        try:
            with write_lock():
                conn.execute("UPDATE items SET archived=1 WHERE id=?", (item_id,))
                add_event(conn, item_id, "archived", "Removed from inventory")
                conn.commit()
        finally:
            conn.close()
        flash("Item removed from inventory. Its sales were kept.", "ok")
        return redirect(url_for("inventory"))

    @app.route("/inventory/bulk-edit", methods=["POST"])
    @login_required
    def inventory_bulk_edit():
        ids = [parse_int(i) for i in request.form.getlist("ids") if parse_int(i)]
        if not ids:
            flash("Tick at least one item.", "error")
            return redirect(request.form.get("back") or url_for("inventory"))
        action = request.form.get("action")
        conn = connect()
        try:
            ph = ",".join("?" * len(ids))
            with write_lock():
                if action == "location":
                    loc = (request.form.get("value") or "").strip() or None
                    conn.execute(f"UPDATE items SET location=? WHERE id IN ({ph})", [loc] + ids)
                    for i in ids:
                        add_event(conn, i, "location", f"Moved to {loc or '—'}")
                elif action == "source":
                    conn.execute(f"UPDATE items SET source=? WHERE id IN ({ph})",
                                 [(request.form.get("value") or "").strip() or None] + ids)
                elif action == "bulk":
                    b = parse_int(request.form.get("bulk_buy_id"))
                    conn.execute(f"UPDATE items SET bulk_buy_id=? WHERE id IN ({ph})", [b] + ids)
                    for i in ids:
                        _sync_item_sales(conn, i)
                elif action == "cost":
                    conn.execute(f"UPDATE items SET cost=? WHERE id IN ({ph})",
                                 [parse_money(request.form.get("value"))] + ids)
                elif action in ("packaging", "handling"):
                    conn.execute(f"UPDATE items SET {action}_cost=? WHERE id IN ({ph})",
                                 [parse_money(request.form.get("value"))] + ids)
                conn.commit()
        finally:
            conn.close()
        flash(f"Updated {len(ids)} item(s).", "ok")
        return redirect(request.form.get("back") or url_for("inventory"))

    # ======================================================================= #
    # Sales from other marketplaces
    # ======================================================================= #
    @app.route("/sales/new", methods=["GET", "POST"])
    @app.route("/sales/manual/<line_id>", methods=["GET", "POST"])
    @login_required
    def manual_sale(line_id=None):
        conn = connect()
        try:
            row = None
            if line_id:
                row = conn.execute("SELECT * FROM sales WHERE line_item_id=? AND source IN "
                                   "('manual', 'cardtrader', 'shopify')", (line_id,)).fetchone()
                if not row:
                    abort(404)
            if request.method == "POST":
                f = request.form
                sold = f.get("sold_at") or date.today().isoformat()
                guard = closed_guard([sold] + ([row["sold_at"]] if row else []), "Recording this sale")
                if guard:
                    return guard
                item_id = parse_int(f.get("item_id"))
                bulk = None
                if item_id:
                    r = conn.execute("SELECT bulk_buy_id FROM items WHERE id=?", (item_id,)).fetchone()
                    bulk = r["bulk_buy_id"] if r else None
                vals = dict(
                    title=(f.get("title") or "").strip() or "Sale", quantity=max(parse_int(f.get("quantity")) or 1, 1),
                    item_price=parse_money(f.get("item_price")) or 0,
                    delivery_charged=parse_money(f.get("delivery_charged")) or 0,
                    manual_fees=parse_money(f.get("manual_fees")) or 0, postage_cost=parse_money(f.get("postage_cost")),
                    packaging_cost=parse_money(f.get("packaging_cost")), cost=parse_money(f.get("cost")),
                    handling_cost=parse_money(f.get("handling_cost")),
                    channel=f.get("channel") or "Other", sold_at=sold + "T12:00:00Z" if len(sold) == 10 else sold,
                    item_id=item_id, bulk_buy_id=bulk, notes=f.get("notes") or None)
                with write_lock():
                    if row:
                        conn.execute("""UPDATE sales SET title=:title, quantity=:quantity, item_price=:item_price,
                            delivery_charged=:delivery_charged, manual_fees=:manual_fees, postage_cost=:postage_cost,
                            packaging_cost=:packaging_cost, handling_cost=:handling_cost, cost=:cost, channel=:channel,
                            sold_at=:sold_at,
                            item_id=:item_id, bulk_buy_id=:bulk_buy_id, notes=:notes WHERE line_item_id=:id""",
                                     {**vals, "id": line_id})
                    else:
                        lid = "M-" + uuid.uuid4().hex[:12]
                        conn.execute("""INSERT INTO sales(line_item_id, order_id, title, quantity, item_price,
                            delivery_charged, manual_fees, postage_cost, packaging_cost, handling_cost, cost, channel, sold_at,
                            item_id, bulk_buy_id, notes, source, currency, updated_at)
                            VALUES (:id, :id, :title, :quantity, :item_price, :delivery_charged, :manual_fees,
                            :postage_cost, :packaging_cost, :handling_cost, :cost, :channel, :sold_at, :item_id, :bulk_buy_id, :notes,
                            'manual', 'GBP', :now)""", {**vals, "id": lid, "now": now_iso()})
                        if item_id:
                            add_event(conn, item_id, "sold",
                                      f"Sold {vals['quantity']} on {vals['channel']} for £{vals['item_price']:,.2f}",
                                      at=vals["sold_at"])
                    conn.commit()
                note = ""
                if not row and item_id and f.get("update_ebay"):
                    import stores
                    note = stores.propagate_sale(conn, lid, item_id, vals["quantity"], vals["channel"])
                    note = ("Also: " + note + ".") if note else ""
                    if vals["channel"] == "Vinted":
                        with write_lock():
                            conn.execute("UPDATE items SET vinted_listed_at=NULL, vinted_url=NULL WHERE id=? AND "
                                         "quantity <= (SELECT COALESCE(SUM(quantity), 0) FROM sales WHERE item_id=?)",
                                         (item_id, item_id))
                            conn.commit()
                flash(("Sale saved. " + note).strip(), "warn" if "couldn't" in note else "ok")
                return redirect(url_for("sales_list"))
            items = load_items(conn, "i.archived = 0")
            items = [i for i in items if i["status"] != "sold" or (row and i["id"] == row["item_id"])]
            ebay_stock = {str(r[0]): r[1] for r in conn.execute(
                """SELECT item_id, SUM(q) FROM (
                       SELECT item_id, COALESCE(quantity_available, 0) AS q FROM listings
                        WHERE status='active' AND item_id IS NOT NULL
                       UNION ALL SELECT item_id, COALESCE(quantity, 0) FROM channel_listings WHERE status='active')
                   GROUP BY item_id HAVING SUM(q) > 0""")}
        finally:
            conn.close()
        return render_template("manual_sale.html", row=dict(row) if row else None, items=items, ebay_stock=ebay_stock,
                               channels=[c for c in CHANNELS if c != "eBay"], today=date.today().isoformat(),
                               preselect=parse_int(request.args.get("item")),
                               channel=request.args.get("channel"))

    @app.route("/sales/manual/<line_id>/delete", methods=["POST"])
    @login_required
    def manual_sale_delete(line_id):
        conn = connect()
        try:
            row = conn.execute("SELECT sold_at FROM sales WHERE line_item_id=? AND source='manual'",
                               (line_id,)).fetchone()
            if not row:
                abort(404)
            guard = closed_guard([row["sold_at"]], "Deleting this sale")
            if guard:
                return guard
            with write_lock():
                conn.execute("DELETE FROM sales WHERE line_item_id=? AND source='manual'", (line_id,))
                conn.commit()
        finally:
            conn.close()
        flash("Sale deleted.", "ok")
        return redirect(url_for("sales_list"))

    # ======================================================================= #
    # Insights
    # ======================================================================= #
    @app.route("/insights")
    @login_required
    def insights():
        key = request.args.get("period", "90d")
        start, end = period_range(key, request.args.get("start"), request.args.get("end"))
        conn = connect()
        try:
            where, params = sales_where(start, end)
            sales = profit.compute_sales(conn, where, tuple(params))
            tot = profit.summarise(sales)
            cogs = tot["item_costs"] + tot["bulk_alloc"]
            net_profit = tot["money_made"] - cogs
            # spend in period
            p_where, p_params = "1=1", []
            if start:
                p_where += " AND purchased_at >= ?"
                p_params.append(start)
            if end:
                p_where += " AND purchased_at < ?"
                p_params.append(end)
            item_spend = conn.execute(f"""SELECT COALESCE(SUM(cost * COALESCE(quantity,1)), 0) FROM items
                                          WHERE bulk_buy_id IS NULL AND cost IS NOT NULL AND {p_where}""",
                                      p_params).fetchone()[0]
            bulk_spend = conn.execute(f"""SELECT COALESCE(SUM(COALESCE(total_cost,0) + COALESCE(extra_costs,0)), 0)
                                          FROM bulk_buys WHERE {p_where}""", p_params).fetchone()[0]
            ded = tot["deductions"]
            fees = sum(v for k, v in ded.items() if k not in ("Refunds", "eBay postage labels", "Postage (paid by you)",
                                                                "Packaging", "Promoted listings"))
            breakdown = {
                "Inventory bought": item_spend + bulk_spend,
                "Cost of goods sold": cogs,
                "Selling fees": fees,
                "Promoted listings": ded.get("Promoted listings", 0),
                "Postage": ded.get("eBay postage labels", 0) + ded.get("Postage (paid by you)", 0),
                "Packaging": ded.get("Packaging", 0),
                "Refunds": ded.get("Refunds", 0),
            }
            # listings vs sales by week
            l_where, l_params = "1=1", []
            if start:
                l_where += " AND start_time >= ?"
                l_params.append(start)
            if end:
                l_where += " AND start_time < ?"
                l_params.append(end)
            new_listings = [r[0] for r in conn.execute(
                f"SELECT start_time FROM listings WHERE relisted_from IS NULL AND {l_where}", l_params)]
            weekly = defaultdict(lambda: {"listed": 0, "sold": 0, "gross": 0.0, "profit": 0.0, "money": 0.0})

            def wk(ts):
                d = _parse(ts)
                if not d:
                    return None
                monday = (d.date() - timedelta(days=d.weekday()))
                return monday.isoformat()
            for ts in new_listings:
                k = wk(ts)
                if k:
                    weekly[k]["listed"] += 1
            for s in sales:
                k = wk(s["sold_at"])
                if not k:
                    continue
                weekly[k]["sold"] += s["quantity"] or 1
                weekly[k]["gross"] += s["gross"]
                weekly[k]["money"] += s["money_made"]
                if s["profit"] is not None:
                    weekly[k]["profit"] += s["profit"]
            weeks = [{"week": k, **{kk: round(vv, 2) if isinstance(vv, float) else vv for kk, vv in v.items()},
                      "margin": round(v["profit"] / v["gross"] * 100, 1) if v["gross"] else None}
                     for k, v in sorted(weekly.items())]
            # time to list / sell
            first_listed = {r["item_id"]: r["fl"] for r in conn.execute(
                "SELECT item_id, MIN(start_time) fl FROM listings WHERE item_id IS NOT NULL GROUP BY item_id")}
            purchased = {r["id"]: r["purchased_at"] for r in conn.execute(
                "SELECT id, purchased_at FROM items WHERE purchased_at IS NOT NULL")}
            to_sell, to_list = [], []
            for s in sales:
                if s.get("item_id") and first_listed.get(s["item_id"]):
                    d = _days(first_listed[s["item_id"]], s["sold_at"])
                    if d is not None and d >= 0:
                        to_sell.append(d)
            for iid, fl in first_listed.items():
                if purchased.get(iid):
                    d = _days(purchased[iid] + "T00:00:00Z", fl)
                    if d is not None and d >= 0 and (not start or (fl or "") >= start):
                        to_list.append(d)
            # by channel
            channels = defaultdict(lambda: {"count": 0, "gross": 0.0, "money": 0.0, "profit": 0.0, "cost": 0.0})
            for s in sales:
                c = channels[s.get("channel") or "eBay"]
                c["count"] += 1
                c["gross"] += s["gross"]
                c["money"] += s["money_made"]
                if s["item_cost"] is not None:
                    c["cost"] += s["item_cost"]
                    c["profit"] += s["profit"]
            # by source
            sources = defaultdict(lambda: {"bought": 0, "spend": 0.0, "sold": 0, "money": 0.0, "profit": 0.0,
                                           "days": [], "listed": 0})
            unit = profit.item_unit_costs(conn)
            for it in conn.execute("""SELECT i.*, b.source AS bulk_source FROM items i
                                      LEFT JOIN bulk_buys b ON b.id=i.bulk_buy_id WHERE i.archived=0"""):
                src = it["source"] or it["bulk_source"] or "Unknown"
                sources[src]["bought"] += it["quantity"] or 1
                if unit.get(it["id"]) is not None:
                    sources[src]["spend"] += unit[it["id"]] * (it["quantity"] or 1)
            for s in sales:
                src = s.get("item_source")
                if not src and s["bulk_buy_id"]:
                    r = conn.execute("SELECT source FROM bulk_buys WHERE id=?", (s["bulk_buy_id"],)).fetchone()
                    src = r["source"] if r else None
                src = src or "Unknown"
                sources[src]["sold"] += s["quantity"] or 1
                sources[src]["money"] += s["money_made"]
                if s["profit"] is not None:
                    sources[src]["profit"] += s["profit"]
                if s.get("item_id") and first_listed.get(s["item_id"]):
                    d = _days(first_listed[s["item_id"]], s["sold_at"])
                    if d is not None and d >= 0:
                        sources[src]["days"].append(d)
            source_rows = []
            for name, v in sources.items():
                source_rows.append({
                    "name": name, "bought": v["bought"], "spend": round(v["spend"], 2), "sold": v["sold"],
                    "money": round(v["money"], 2), "profit": round(v["profit"], 2),
                    "roi": (v["profit"] / v["spend"] * 100) if v["spend"] else None,
                    "sell_through": (min(v["sold"] / v["bought"], 1) * 100) if v["bought"] else None,
                    "avg_days": (sum(v["days"]) / len(v["days"])) if v["days"] else None})
            source_rows.sort(key=lambda r: -r["profit"])
            # buyer locations
            geo = defaultdict(int)
            countries = defaultdict(int)
            for s in sales:
                if s.get("ship_country"):
                    countries[s["ship_country"]] += 1
                    if s["ship_country"] == "GB" and s.get("ship_outcode"):
                        geo[s["ship_outcode"]] += 1
            active_listings = conn.execute("SELECT COUNT(*) FROM listings WHERE status='active'").fetchone()[0]
            list_value = conn.execute("""SELECT COALESCE(SUM(price * COALESCE(quantity_available,1)),0)
                                         FROM listings WHERE status='active'""").fetchone()[0]
        finally:
            conn.close()
        kpi = {
            "gross": tot["gross"], "money_made": tot["money_made"], "net_profit": round(net_profit, 2),
            "margin": (net_profit / tot["gross"] * 100) if tot["gross"] else None,
            "roi": (net_profit / cogs * 100) if cogs else None, "cogs": round(cogs, 2),
            "avg_sale": (tot["gross"] / tot["count"]) if tot["count"] else None,
            "days_to_sell": (sum(to_sell) / len(to_sell)) if to_sell else None,
            "days_to_list": (sum(to_list) / len(to_list)) if to_list else None,
            "new_listings": len(new_listings), "units": tot["units"], "missing_cost": tot["missing_cost"],
            "active_listings": active_listings, "list_value": list_value,
        }
        import timetosell
        conn = connect()
        try:
            tts = timetosell.compute(conn, start=start[:10] if start else None, end=end[:10] if end else None)
        finally:
            conn.close()
        kpi["days_to_sell"] = tts["single"]["avg"]
        kpi["sell_every"] = tts["multi"]["every_days"]
        return render_template("insights.html", tts=tts, kpi=kpi, weeks=weeks, breakdown=breakdown, channels=dict(channels),
                               sources=source_rows, geo=dict(geo), countries=dict(countries), period=key,
                               periods=PERIODS, start=request.args.get("start", ""), end=request.args.get("end", ""))

    # ======================================================================= #
    # Automations
    # ======================================================================= #
    @app.route("/automations")
    @login_required
    def automations():
        conn = connect()
        try:
            automation.ensure_rules(conn)
            rules = []
            for r in conn.execute("SELECT * FROM automation_rules ORDER BY id"):
                r = dict(r)
                r["cfg"] = automation.rule_config(r)
                if r["kind"] == "relist":
                    r["upcoming"] = automation.relist_candidates(conn, r["cfg"], horizon_hours=24 * 7)[:40]
                else:
                    r["upcoming"] = automation.promote_candidates(conn, r["cfg"], horizon_days=7)[:40]
                rules.append(r)
            queue = [dict(r) for r in conn.execute("""SELECT q.*, l.item_id FROM relist_queue q
                       LEFT JOIN listings l ON l.legacy_item_id = q.legacy_item_id
                       WHERE q.status IN ('pending', 'approved') ORDER BY q.id""")]
            today_count = automation.relisted_today(conn)
            log = [dict(r) for r in conn.execute("""SELECT a.*, l.title FROM automation_log a
                       LEFT JOIN listings l ON l.legacy_item_id = a.legacy_item_id
                       ORDER BY a.id DESC LIMIT 60""")]
            active = conn.execute("SELECT COUNT(*) FROM listings WHERE status='active'").fetchone()[0]
        finally:
            conn.close()
        from ebay_api import EbayClient
        can_promote = True if app.config["DEMO"] else EbayClient().can_promote()
        st = Settings().get_many(["automations_paused", "automation_runner"])
        return render_template("automations.html", rules=rules, log=log, paused=st.get("automations_paused") == "1",
                               runner=st.get("automation_runner"), state=automation.STATE, active=active,
                               can_promote=can_promote, labels=automation.LABELS, queue=queue,
                               today_count=today_count)

    @app.route("/automations/queue", methods=["POST"])
    @login_required
    def automation_queue():
        action = request.form.get("action")
        ids = [parse_int(i) for i in request.form.getlist("ids") if parse_int(i)]
        conn = connect()
        try:
            with write_lock():
                where = "status='pending'" + (f" AND id IN ({','.join('?' * len(ids))})" if ids else "")
                new = "approved" if action == "approve" else "rejected"
                n = conn.execute(f"UPDATE relist_queue SET status=? WHERE {where}", [new] + ids).rowcount
                conn.commit()
        finally:
            conn.close()
        if action == "approve" and n:
            rid = parse_int(request.form.get("rule_id"))
            threading.Thread(target=automation.run_all, kwargs={
                "only_rule": rid, "simulate": app.config["DEMO"], "triggered_by": "manual"}, daemon=True).start()
            flash(f"Approved {n} relist{'s' if n != 1 else ''} — relisting now.", "ok")
        else:
            flash(f"Skipped {n} relist{'s' if n != 1 else ''}. They won't be suggested again for these listings.", "ok")
        return redirect(url_for("automations"))

    @app.route("/automations/<int:rule_id>", methods=["POST"])
    @login_required
    def automation_save(rule_id):
        conn = connect()
        try:
            r = conn.execute("SELECT * FROM automation_rules WHERE id=?", (rule_id,)).fetchone()
            if not r:
                abort(404)
            cfg = automation.rule_config(r)
            for k, default in automation.DEFAULTS[r["kind"]].items():
                if isinstance(default, bool):
                    cfg[k] = bool(request.form.get(k))
                elif isinstance(default, (int, float)):
                    v = parse_float(request.form.get(k))
                    if v is not None:
                        cfg[k] = max(v, 0) if isinstance(default, float) else int(max(v, 0))
                else:
                    cfg[k] = (request.form.get(k) or default).strip()
            if r["kind"] == "promote":
                cfg["ad_rate"] = min(max(cfg["ad_rate"], 2.0), 100.0)   # eBay's allowed range for general ads
            if r["kind"] == "relist":
                cfg["price_drop_pct"] = min(cfg["price_drop_pct"], 50.0)
                cfg["max_total_drop_pct"] = min(cfg["max_total_drop_pct"], 90.0)
                hv = parse_int(request.form.get("relist_hour"))
                cfg["relist_hour"] = hv if hv is not None and 0 <= hv <= 23 else -1
                cfg["max_per_day"] = min(max(cfg["max_per_day"], 1), 200)
                cfg["max_cycles"] = min(max(cfg["max_cycles"], 0), 20)
                cfg["mode"] = "age" if request.form.get("mode") == "age" else "renewal"
            with write_lock():
                conn.execute("UPDATE automation_rules SET enabled=?, config=? WHERE id=?",
                             (1 if request.form.get("enabled") else 0, json.dumps(cfg), rule_id))
                conn.commit()
        finally:
            conn.close()
        flash("Automation saved." + (" It's on and will run automatically every 30 minutes."
                                     if request.form.get("enabled") else " It's switched off."), "ok")
        return redirect(url_for("automations"))

    @app.route("/automations/pause", methods=["POST"])
    @login_required
    def automation_pause():
        paused = request.form.get("paused") == "1"
        Settings().set("automations_paused", "1" if paused else "0")
        flash("All automations paused." if paused else "Automations resumed.", "ok")
        return redirect(url_for("automations"))

    @app.route("/automations/run", methods=["POST"])
    @login_required
    def automation_run():
        rid = parse_int(request.form.get("rule_id"))
        threading.Thread(target=automation.run_all, kwargs={
            "only_rule": rid, "simulate": app.config["DEMO"], "triggered_by": "manual"}, daemon=True).start()
        flash("Running now — refresh in a minute to see the results in the log.", "ok")
        return redirect(url_for("automations"))

    # ======================================================================= #
    # UK tax
    # ======================================================================= #
    def _current_tax_year():
        return tax_year_of(date.today())

    @app.route("/tax")
    @login_required
    def tax():
        year = parse_int(request.args.get("year")) or _current_tax_year()
        st = Settings()
        basis = request.args.get("basis") or st.get("tax_basis") or "cash"
        if request.args.get("basis") in ("cash", "accrual"):
            st.set("tax_basis", basis)
        conn = connect()
        try:
            r = taxcalc.compute(conn, year, basis)
            first = conn.execute("SELECT MIN(sold_at) FROM sales").fetchone()[0]
        finally:
            conn.close()
        first_year = tax_year_of(first) or year
        years = list(range(_current_tax_year(), min(first_year, year) - 1, -1))
        return render_template("tax.html", r=r, years=[(y, tax_year_label(y)) for y in years], year=year,
                               closed=year in closed_years(), box_labels=taxcalc.BOX_LABELS)

    @app.route("/tax/close", methods=["POST"])
    @login_required
    def tax_close():
        year = parse_int(request.form.get("year"))
        ys = closed_years()
        if request.form.get("closed") == "1":
            ys.add(year)
        else:
            ys.discard(year)
        Settings().set("closed_tax_years", ",".join(str(y) for y in sorted(ys)))
        flash(f"{tax_year_label(year)} marked as {'closed — edits that change it now ask first' if year in ys else 'open'}.",
              "ok")
        return redirect(url_for("tax", year=year))

    @app.route("/tax/export.csv")
    @login_required
    def tax_export():
        year = parse_int(request.args.get("year")) or _current_tax_year()
        basis = Settings().get("tax_basis") or "cash"
        conn = connect()
        try:
            r = taxcalc.compute(conn, year, basis)
            expenses = [dict(e) for e in conn.execute("SELECT * FROM expenses WHERE date >= ? AND date < ? ORDER BY date",
                                                      (r["start"], r["end"]))]
            miles = [dict(m) for m in conn.execute("SELECT * FROM mileage WHERE date >= ? AND date < ? ORDER BY date",
                                                   (r["start"], r["end"]))]
        finally:
            conn.close()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([f"Self-employment summary {r['label']} ({basis} basis)"])
        w.writerow(["Box", "Description", "Amount"])
        w.writerow([9, "Turnover", r["box9"]])
        w.writerow([10, "Other business income", r["box10"]])
        for b in range(11, 20):
            w.writerow([b, taxcalc.BOX_LABELS[b], r["boxes"][b]])
        w.writerow([20, "Total allowable expenses", r["box20"]])
        w.writerow([21, "Net profit", r["box21"]])
        w.writerow([22, "Net loss", r["box22"]])
        w.writerow([])
        w.writerow(["SALES"])
        w.writerow(["Date", "Channel", "Order", "Title", "Qty", "Buyer paid", "Refunds", "Fees & charges",
                    "Your postage/packaging", "Cost of item", "Profit", "Status"])
        for s in sorted(r["sales"], key=lambda s: s["sold_at"] or ""):
            refunds = s["ebay_deductions"].get("Refunds", 0)
            w.writerow([(s["sold_at"] or "")[:10], s.get("channel") or "eBay", s["order_id"], s["title"], s["quantity"],
                        s["gross"], refunds, round(s["ebay_deductions_total"] - refunds, 2), s["own_costs_total"],
                        "" if s["item_cost"] is None else s["item_cost"],
                        "" if s["profit"] is None else s["profit"], s.get("status") or ""])
        w.writerow([])
        w.writerow(["EXPENSES"])
        w.writerow(["Date", "Category", "SA103S box", "Description", "Amount"])
        for e in expenses:
            w.writerow([e["date"], taxcalc.CATEGORY_LABEL.get(e["category"], e["category"]),
                        taxcalc.CATEGORY_BOX.get(e["category"], 19), e["description"], e["amount"]])
        w.writerow([])
        w.writerow(["MILEAGE"])
        w.writerow(["Date", "Vehicle", "Miles", "Purpose", "Route"])
        for m in miles:
            w.writerow([m["date"], m["vehicle"], m["miles"], m["purpose"], m["route"]])
        w.writerow(["", "", r["mileage"]["miles"], "Allowance", r["mileage"]["total"]])
        return Response(buf.getvalue(), mimetype="text/csv", headers={
            "Content-Disposition": f"attachment; filename=self_assessment_{r['label'].replace('/', '-')}.csv"})

    @app.route("/expenses", methods=["GET", "POST"])
    @login_required
    def expenses():
        year = parse_int(request.args.get("year")) or _current_tax_year()
        conn = connect()
        try:
            if request.method == "POST":
                eid = parse_int(request.form.get("id"))
                d = request.form.get("date") or date.today().isoformat()
                old = conn.execute("SELECT date FROM expenses WHERE id=?", (eid,)).fetchone() if eid else None
                guard = closed_guard([d] + ([old["date"]] if old else []), "This expense")
                if guard:
                    return guard
                vals = (d, request.form.get("category") or "other", (request.form.get("description") or "").strip(),
                        parse_money(request.form.get("amount")) or 0, parse_int(request.form.get("item_id")))
                import features2
                with write_lock():
                    if request.form.get("delete") and eid:
                        conn.execute("DELETE FROM expenses WHERE id=?", (eid,))
                    elif eid:
                        conn.execute("UPDATE expenses SET date=?, category=?, description=?, amount=?, item_id=? "
                                     "WHERE id=?", vals + (eid,))
                    else:
                        eid = conn.execute("INSERT INTO expenses(date, category, description, amount, item_id, "
                                           "created_at) VALUES (?,?,?,?,?,?)", vals + (now_iso(),)).lastrowid
                    f = request.files.get("receipt")
                    if f and f.filename and not request.form.get("delete"):
                        try:
                            rel = features2.save_receipt(f, eid, d)
                            conn.execute("UPDATE expenses SET receipt_path=? WHERE id=?", (rel, eid))
                        except ValueError as ex:
                            flash(str(ex), "error")
                    conn.commit()
                flash("Expense saved." if not request.form.get("delete") else "Expense deleted.", "ok")
                return redirect(url_for("expenses", year=tax_year_of(d)))
            from common import tax_year_range
            s, e = tax_year_range(year)
            rows = [dict(r) for r in conn.execute("""SELECT x.*, i.title AS item_title FROM expenses x
                        LEFT JOIN items i ON i.id = x.item_id WHERE date >= ? AND date < ? ORDER BY date DESC""",
                                                  (s, e))]
            items = [dict(r) for r in conn.execute("SELECT id, title FROM items WHERE archived=0 ORDER BY title")]
        finally:
            conn.close()
        by_cat = defaultdict(float)
        for r in rows:
            by_cat[r["category"]] += r["amount"] or 0
        return render_template("expenses.html", rows=rows, items=items, year=year, label=tax_year_label(year),
                               cats=taxcalc.EXPENSE_CATEGORIES, cat_label=taxcalc.CATEGORY_LABEL,
                               cat_box=taxcalc.CATEGORY_BOX, by_cat=dict(by_cat), total=sum(by_cat.values()),
                               today=date.today().isoformat(), years=_year_options())

    @app.route("/mileage", methods=["GET", "POST"])
    @login_required
    def mileage():
        year = parse_int(request.args.get("year")) or _current_tax_year()
        conn = connect()
        try:
            if request.method == "POST":
                mid = parse_int(request.form.get("id"))
                d = request.form.get("date") or date.today().isoformat()
                old = conn.execute("SELECT date FROM mileage WHERE id=?", (mid,)).fetchone() if mid else None
                guard = closed_guard([d] + ([old["date"]] if old else []), "This journey")
                if guard:
                    return guard
                miles = parse_float(request.form.get("miles")) or 0
                if request.form.get("return_trip"):
                    miles *= 2
                vals = (d, miles, request.form.get("vehicle") or "car", (request.form.get("purpose") or "").strip(),
                        (request.form.get("route") or "").strip())
                with write_lock():
                    if request.form.get("delete") and mid:
                        conn.execute("DELETE FROM mileage WHERE id=?", (mid,))
                    elif mid:
                        conn.execute("UPDATE mileage SET date=?, miles=?, vehicle=?, purpose=?, route=? WHERE id=?",
                                     vals + (mid,))
                    else:
                        conn.execute("INSERT INTO mileage(date, miles, vehicle, purpose, route, created_at) "
                                     "VALUES (?,?,?,?,?,?)", vals + (now_iso(),))
                    conn.commit()
                flash("Journey saved." if not request.form.get("delete") else "Journey deleted.", "ok")
                return redirect(url_for("mileage", year=tax_year_of(d)))
            from common import tax_year_range
            s, e = tax_year_range(year)
            rows = [dict(r) for r in conn.execute("SELECT * FROM mileage WHERE date >= ? AND date < ? ORDER BY date DESC",
                                                  (s, e))]
            import dispatch
            days = dispatch.dispatch_days(conn, s, e)
            locs = dispatch.locations(conn)
            loc_names = {l["id"]: l["name"] for l in locs}
        finally:
            conn.close()
        allowance = taxcalc.mileage_allowance(rows, year)
        car_rate = taxcalc.mileage_rates("car", year)
        st = Settings().get_many(["home_postcode", "auto_dropoff_trips", "mileage_vehicle"])
        unlogged = [d for d in days if not d["logged"]]
        return render_template("mileage.html", rows=rows, year=year, label=tax_year_label(year), allowance=allowance,
                               car_rate=car_rate, today=date.today().isoformat(), years=_year_options(), days=days,
                               unlogged=unlogged, locs=locs, loc_names=loc_names, st=st, closed=year in closed_years())

    @app.route("/mileage/settings", methods=["POST"])
    @login_required
    def mileage_settings():
        import dispatch
        st = Settings()
        pc = dispatch.normalise_postcode(request.form.get("home_postcode") or "")
        old = st.get("home_postcode")
        auto = "1" if request.form.get("auto_dropoff_trips") else "0"
        vals = {"home_postcode": pc or None, "auto_dropoff_trips": auto,
                "mileage_vehicle": request.form.get("mileage_vehicle") or "car"}
        if auto == "1" and st.get("auto_dropoff_trips") != "1":
            vals["auto_dropoff_since"] = date.today().isoformat()
        st.set_many(vals)
        msg = "Saved."
        if pc and pc != old:
            # home moved: recalculate every drop-off point
            conn = connect()
            try:
                n, errs = 0, []
                for l in dispatch.locations(conn):
                    if not l["postcode"]:
                        continue
                    try:
                        miles, note = dispatch.driving_miles(pc, l["postcode"])
                        with write_lock():
                            conn.execute("UPDATE drop_off_locations SET miles_one_way=?, distance_note=? WHERE id=?",
                                         (miles, note, l["id"]))
                            conn.commit()
                        n += 1
                    except dispatch.DistanceError as e:
                        errs.append(f"{l['name']}: {e}")
                if n:
                    msg += f" Recalculated {n} drop-off distance{'s' if n != 1 else ''}."
                if errs:
                    flash("; ".join(errs), "error")
            finally:
                conn.close()
        flash(msg, "ok")
        return redirect(request.form.get("back") or url_for("mileage"))

    @app.route("/mileage/locations", methods=["POST"])
    @login_required
    def mileage_locations():
        import dispatch
        f = request.form
        action = f.get("action", "add")
        lid = parse_int(f.get("id"))
        home = Settings().get("home_postcode")
        conn = connect()
        try:
            with write_lock():
                if action == "delete" and lid:
                    conn.execute("DELETE FROM drop_off_locations WHERE id=?", (lid,))
                    flash("Drop-off point removed. Trips already logged keep their miles.", "ok")
                elif action == "default" and lid:
                    conn.execute("UPDATE drop_off_locations SET is_default = (id = ?)", (lid,))
                    flash("Default drop-off point changed.", "ok")
                elif action == "miles" and lid:
                    miles = parse_float(f.get("miles_one_way"))
                    conn.execute("UPDATE drop_off_locations SET miles_one_way=?, distance_note='entered by you' "
                                 "WHERE id=?", (miles, lid))
                    flash("Distance updated.", "ok")
                else:
                    name = (f.get("name") or "").strip() or "Post office"
                    pc = dispatch.normalise_postcode(f.get("postcode") or "")
                    miles = parse_float(f.get("miles_one_way"))
                    note = "entered by you" if miles else None
                    if not miles:
                        if not home:
                            flash("Add your home postcode first so the distance can be worked out, "
                                  "or type the miles yourself.", "error")
                            return redirect(url_for("mileage"))
                        try:
                            miles, note = dispatch.driving_miles(home, pc)
                        except dispatch.DistanceError as e:
                            flash(f"{e} Type the one-way miles yourself instead.", "error")
                            return redirect(url_for("mileage"))
                    first = not conn.execute("SELECT 1 FROM drop_off_locations").fetchone()
                    conn.execute("""INSERT INTO drop_off_locations(name, postcode, miles_one_way, distance_note,
                                    is_default, created_at) VALUES (?,?,?,?,?,?)""",
                                 (name, pc or None, miles, note, 1 if first or f.get("is_default") else 0, now_iso()))
                    if f.get("is_default") and not first:
                        new_id = conn.execute("SELECT MAX(id) FROM drop_off_locations").fetchone()[0]
                        conn.execute("UPDATE drop_off_locations SET is_default = (id = ?)", (new_id,))
                    flash(f"Added {name}: {miles:.1f} miles each way ({note}).", "ok")
                conn.commit()
        finally:
            conn.close()
        return redirect(url_for("mileage"))

    @app.route("/mileage/from-dispatches", methods=["POST"])
    @login_required
    def mileage_from_dispatches():
        import dispatch
        dates = request.form.getlist("dates")
        if not dates:
            flash("Tick at least one dispatch day.", "error")
            return redirect(request.form.get("back") or url_for("mileage"))
        guard = closed_guard(dates, "Adding these journeys")
        if guard:
            return guard
        conn = connect()
        try:
            loc_id = parse_int(request.form.get("location_id"))
            loc = conn.execute("SELECT * FROM drop_off_locations WHERE id=?", (loc_id,)).fetchone()
            try:
                n = dispatch.log_trips(conn, dates, dict(loc) if loc else None,
                                       Settings().get("mileage_vehicle") or "car")
            except dispatch.DistanceError as e:
                flash(str(e), "error")
                return redirect(request.form.get("back") or url_for("mileage"))
        finally:
            conn.close()
        flash(f"Logged {n} post office trip{'s' if n != 1 else ''}.", "ok")
        return redirect(request.form.get("back") or url_for("mileage"))

    def _year_options():
        cur = _current_tax_year()
        return [(y, tax_year_label(y)) for y in range(cur, cur - 6, -1)]

    # ======================================================================= #
    # Notifications
    # ======================================================================= #
    @app.route("/notifications")
    @login_required
    def notifications():
        kind = request.args.get("kind", "")
        conn = connect()
        try:
            q = "SELECT * FROM notifications" + (" WHERE kind=?" if kind else "") + " ORDER BY at DESC, id DESC LIMIT 300"
            rows = [dict(r) for r in conn.execute(q, (kind,) if kind else ())]
            counts = {r["kind"]: r["n"] for r in conn.execute(
                "SELECT kind, COUNT(*) n FROM notifications WHERE read=0 GROUP BY kind")}
        finally:
            conn.close()
        return render_template("notifications.html", rows=rows, kind=kind, counts=counts)

    @app.route("/notifications/read", methods=["POST"])
    @login_required
    def notifications_read():
        conn = connect()
        try:
            with write_lock():
                nid = parse_int(request.form.get("id"))
                if nid:
                    conn.execute("UPDATE notifications SET read=1 WHERE id=?", (nid,))
                else:
                    conn.execute("UPDATE notifications SET read=1")
                conn.commit()
        finally:
            conn.close()
        return redirect(request.form.get("back") or url_for("notifications"))

    @app.route("/notifications/latest")
    @login_required
    def notifications_latest():
        after = parse_int(request.args.get("after")) or 0
        conn = connect()
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT id, kind, title, body, link FROM notifications WHERE id > ? ORDER BY id LIMIT 20", (after,))]
            unread = conn.execute("SELECT COUNT(*) FROM notifications WHERE read=0").fetchone()[0]
            max_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM notifications").fetchone()[0]
        finally:
            conn.close()
        return jsonify({"items": rows, "unread": unread, "max_id": max_id})
