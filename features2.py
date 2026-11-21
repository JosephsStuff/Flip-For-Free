"""P&L statements, receipts, CSV import wizard, store health, Growth Lab, goals, calculators, phone access."""
import csv
import io
import json
import mimetypes
import os
import re
import secrets
import socket
import time
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from flask import Response, abort, flash, jsonify, redirect, render_template, request, send_file, session, url_for

import db
import profit
import taxcalc
from common import (closed_guard, fy_label, fy_of, fy_range, fy_start_md, login_required, parse_float, parse_int,
                    parse_money, tax_year_of)
from db import Settings, add_event, connect, now_iso, write_lock


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
    return None if not a or not b else (b - a).total_seconds() / 86400


def _r(x):
    return round((x or 0) + 0.0, 2)


# --------------------------------------------------------------------------- #
# Profit & loss
# --------------------------------------------------------------------------- #
SHIPPING = {"eBay postage labels", "Postage (paid by you)", "Packaging"}


def mileage_amount(rows) -> float:
    """HMRC simplified mileage across any date range: the 10,000-mile tier resets each tax year."""
    total = 0.0
    by_year = defaultdict(list)
    for r in rows:
        by_year[tax_year_of(r["date"])].append(r)
    for y, rs in by_year.items():
        used = 0.0
        for r in sorted(rs, key=lambda r: r["date"]):
            hi, lo = taxcalc.mileage_rates(r["vehicle"] or "car", y)
            miles = r["miles"] or 0
            if (r["vehicle"] or "car") in ("car", "van"):
                first = max(min(miles, 10000 - used), 0)
                total += first * hi + (miles - first) * lo
                used += miles
            else:
                total += miles * hi
    return round(total, 2)


def pnl(conn, start, end) -> dict:
    sales = profit.compute_sales(conn, "s.sold_at >= ? AND s.sold_at < ?", (start, end))
    items = sum(s["item_price"] or 0 for s in sales)
    postage_in = sum(s["delivery_charged"] or 0 for s in sales)
    refunds = sum(s["ebay_deductions"].get("Refunds", 0) for s in sales)
    revenue = items + postage_in - refunds
    cogs = sum(s["item_cost"] or 0 for s in sales)
    missing = sum(1 for s in sales if s["item_cost"] is None and s.get("status") != "cancelled")
    fees = defaultdict(float)
    promoted = shipping = other = labour = 0.0
    for s in sales:
        for k, v in list(s["ebay_deductions"].items()) + list(s["own_costs"].items()):
            if k == "Refunds":
                continue
            if k == profit.LABOUR:
                labour += v
            elif k == "Promoted listings":
                promoted += v
            elif k in SHIPPING:
                shipping += v
            elif k == "Other costs":
                other += v
            else:
                fees[k] += v
    fee_total = sum(fees.values())
    item_profit = revenue - cogs - fee_total - promoted - shipping - other - labour
    acct = profit.account_charges(conn, start, end)
    exp = defaultdict(float)
    receipts = 0
    for e in conn.execute("SELECT * FROM expenses WHERE date >= ? AND date < ?", (start[:10], end[:10])):
        exp[taxcalc.CATEGORY_LABEL.get(e["category"], "Other")] += e["amount"] or 0
        receipts += 1 if e["receipt_path"] else 0
    miles = [dict(r) for r in conn.execute("SELECT * FROM mileage WHERE date >= ? AND date < ?",
                                           (start[:10], end[:10]))]
    mil = mileage_amount(miles)
    if mil:
        exp["Mileage (HMRC simplified rate)"] += mil
    exp_total = sum(exp.values()) + acct["total"]
    wo = profit.write_offs(conn, start[:10], end[:10])
    wo_total = sum(w["value"] for w in wo)
    net = item_profit - exp_total - wo_total
    return {
        "start": start, "end": end, "sales": len(sales), "units": sum(s["quantity"] or 1 for s in sales),
        "item_sales": _r(items), "postage_in": _r(postage_in), "refunds": _r(refunds), "revenue": _r(revenue),
        "cogs": _r(cogs), "missing_cost": missing,
        "fees": {k: _r(v) for k, v in sorted(fees.items(), key=lambda kv: -kv[1]) if abs(v) >= 0.005},
        "fee_total": _r(fee_total), "promoted": _r(promoted), "shipping": _r(shipping), "other": _r(other),
        "labour": _r(labour), "labour_paid": Settings().get("labour_paid") == "1",
        "item_profit": _r(item_profit), "account": acct, "expenses": {k: _r(v) for k, v in
                                                                     sorted(exp.items(), key=lambda kv: -kv[1])},
        "expense_total": _r(exp_total), "write_offs": wo, "write_off_total": _r(wo_total), "net": _r(net),
        "margin": (item_profit / revenue * 100) if revenue else None,
        "net_margin": (net / revenue * 100) if revenue else None, "receipts": receipts,
        "miles": sum(m["miles"] or 0 for m in miles),
    }


def pnl_period(args):
    """(start, end, label, mode, year, quarter) from request args."""
    mode = args.get("mode", "fy")
    year = parse_int(args.get("year")) or fy_of()
    if mode.startswith("r:"):
        from common import find_range
        r = find_range(mode)
        if r:
            a = date.fromisoformat(r["start"]) if r.get("start") else date(2000, 1, 1)
            b = (date.fromisoformat(r["end"]) if r.get("end") else date.today()) + timedelta(days=1)
            return a.isoformat(), b.isoformat(), f"{r['name']} ({a:%d %b %Y} – {b - timedelta(days=1):%d %b %Y})", \
                mode, year, None
        mode = "fy"
    if mode == "quarter":
        q = min(max(parse_int(args.get("q")) or 1, 1), 4)
        m, d = fy_start_md()
        s = date(year, m, d)
        starts = [_add_months(s, 3 * i) for i in range(5)]
        a, b = starts[q - 1], starts[q]
        return a.isoformat(), b.isoformat(), f"Q{q} {fy_label(year)} ({a:%d %b} – {b - timedelta(days=1):%d %b %Y})", mode, year, q
    if mode == "custom":
        try:
            a = date.fromisoformat(args.get("start"))
            b = date.fromisoformat(args.get("end")) + timedelta(days=1)
        except (TypeError, ValueError):
            a, b = date.today().replace(day=1), date.today() + timedelta(days=1)
        return a.isoformat(), b.isoformat(), f"{a:%d %b %Y} – {b - timedelta(days=1):%d %b %Y}", mode, year, None
    a, b = fy_range(year)
    return a, b, f"Financial year {fy_label(year)}", "fy", year, None


def _add_months(d, n):
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    import calendar
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


# --------------------------------------------------------------------------- #
# Store health / Growth Lab / goals
# --------------------------------------------------------------------------- #
HEALTH_DEFAULTS = {"str": 40.0, "asp": 20.0, "days": 45.0, "stale": 20.0, "stale_days": 60}


def health_targets():
    raw = Settings().get("health_targets")
    t = dict(HEALTH_DEFAULTS)
    if raw:
        try:
            t.update(json.loads(raw))
        except ValueError:
            pass
    return t


def first_listed_map(conn):
    return {r["item_id"]: r["fl"] for r in conn.execute(
        "SELECT item_id, MIN(start_time) fl FROM listings WHERE item_id IS NOT NULL GROUP BY item_id")}


def store_health(conn, days=90):
    t = health_targets()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    sales = profit.compute_sales(conn, "s.sold_at >= ? AND COALESCE(s.status, '') <> 'cancelled'", (since,))
    units = sum(s["quantity"] or 1 for s in sales)
    gross_items = sum(s["item_price"] or 0 for s in sales)
    active = conn.execute("SELECT COUNT(*) FROM listings WHERE status='active'").fetchone()[0]
    fl = first_listed_map(conn)
    d2s = [d for d in (_days(fl.get(s.get("item_id")), s["sold_at"]) for s in sales if s.get("item_id")) if d is not None and d >= 0]
    stale = stale_listings(conn, t["stale_days"])
    metrics = [
        {"key": "str", "name": "Sell-through rate", "value": (units / (units + active) * 100) if (units + active) else None,
         "fmt": "pct", "target": t["str"], "better": "higher",
         "help": f"Items sold in {days} days ÷ (sold + still listed)"},
        {"key": "asp", "name": "Average sale price", "value": (gross_items / units) if units else None, "fmt": "gbp",
         "target": t["asp"], "better": "higher", "help": "Item price per unit, excluding postage"},
        {"key": "days", "name": "Days to sell", "value": (sum(d2s) / len(d2s)) if d2s else None, "fmt": "days",
         "target": t["days"], "better": "lower", "help": "From first listed to sold (relists don't reset it)"},
        {"key": "stale", "name": "Stale listings", "value": (len(stale["listed"]) / active * 100) if active else None,
         "fmt": "pct", "target": t["stale"], "better": "lower",
         "help": f"Share of live listings older than {t['stale_days']} days"},
    ]
    for m in metrics:
        if m["value"] is None:
            m["ok"] = None
        else:
            m["ok"] = m["value"] >= m["target"] if m["better"] == "higher" else m["value"] <= m["target"]
    return {"metrics": metrics, "units": units, "active": active, "stale": stale, "targets": t, "days": days}


def stale_listings(conn, stale_days):
    now = datetime.now(timezone.utc)
    unit = profit.item_unit_costs(conn)
    fl = first_listed_map(conn)
    listed, unlisted = [], []
    for L in conn.execute("""SELECT l.*, i.location, i.purchased_at FROM listings l LEFT JOIN items i ON i.id=l.item_id
                             WHERE l.status='active'"""):
        start = _parse(fl.get(L["item_id"]) or L["start_time"])
        if not start:
            continue
        age = (now - start).days
        if age >= stale_days:
            cost = unit.get(L["item_id"])
            left = L["quantity_available"] or 1
            listed.append({**dict(L), "age": age, "cost": None if cost is None else _r(cost * left),
                           "value": _r((L["price"] or 0) * left)})
    for it in conn.execute("""SELECT i.* FROM items i WHERE i.archived=0 AND i.written_off_at IS NULL
                              AND NOT EXISTS (SELECT 1 FROM listings l WHERE l.item_id=i.id AND l.status='active')
                              AND (SELECT COALESCE(SUM(quantity),0) FROM sales s WHERE s.item_id=i.id) < COALESCE(i.quantity,1)
                              AND COALESCE(i.purchased_at, i.created_at) IS NOT NULL"""):
        start = _parse(it["purchased_at"] + "T00:00:00Z" if it["purchased_at"] else it["created_at"])
        if not start:
            continue
        age = (now - start).days
        if age >= stale_days:
            cost = unit.get(it["id"])
            unlisted.append({**dict(it), "age": age, "cost": None if cost is None else _r(cost * (it["quantity"] or 1))})
    listed.sort(key=lambda r: -r["age"])
    unlisted.sort(key=lambda r: -r["age"])
    tied = sum(r["cost"] or 0 for r in listed) + sum(r["cost"] or 0 for r in unlisted)
    return {"listed": listed, "unlisted": unlisted, "tied_up": _r(tied), "days": stale_days}


BANDS = [(0, 5), (5, 10), (10, 20), (20, 50), (50, 100), (100, None)]


def growth_lab(conn, days=365):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    sales = profit.compute_sales(conn, "s.sold_at >= ? AND COALESCE(s.status, '') <> 'cancelled'", (since,))
    fl = first_listed_map(conn)
    bands = []
    for lo, hi in BANDS:
        ss = [s for s in sales if s["unit_price"] >= lo and (hi is None or s["unit_price"] < hi)]
        costed = [s for s in ss if s["profit"] is not None]
        rev = sum(s["gross"] for s in ss)
        prof = sum(s["profit"] for s in costed)
        rev_c = sum(s["gross"] for s in costed)
        d2s = [d for d in (_days(fl.get(s.get("item_id")), s["sold_at"]) for s in ss if s.get("item_id"))
               if d is not None and d >= 0]
        bands.append({"label": f"£{lo}–£{hi}" if hi else f"£{lo}+", "count": len(ss), "revenue": _r(rev),
                      "profit": _r(prof), "avg_profit": (prof / len(costed)) if costed else None,
                      "margin": (prof / rev_c * 100) if rev_c else None,
                      "days": (sum(d2s) / len(d2s)) if d2s else None})
    best = max((b for b in bands if b["count"]), key=lambda b: b["profit"], default=None)
    best_margin = max((b for b in bands if b["margin"] is not None and b["count"] >= 3),
                      key=lambda b: b["margin"], default=None)
    margin_buckets = [("Loss", None, 0), ("0–20%", 0, 20), ("20–40%", 20, 40), ("40–60%", 40, 60), ("60%+", 60, None)]
    mh = []
    costed = [s for s in sales if s["profit"] is not None and s["gross"]]
    for name, lo, hi in margin_buckets:
        n = sum(1 for s in costed if (lo is None or s["profit"] / s["gross"] * 100 >= lo)
                and (hi is None or s["profit"] / s["gross"] * 100 < hi))
        mh.append({"label": name, "count": n, "pct": (n / len(costed) * 100) if costed else 0})
    day_buckets = [("Within a week", 0, 8), ("8–30 days", 8, 31), ("31–90 days", 31, 91), ("Over 90 days", 91, None)]
    dd = [d for d in (_days(fl.get(s.get("item_id")), s["sold_at"]) for s in sales if s.get("item_id"))
          if d is not None and d >= 0]
    dh = [{"label": n, "count": sum(1 for d in dd if d >= lo and (hi is None or d < hi)),
           "pct": (sum(1 for d in dd if d >= lo and (hi is None or d < hi)) / len(dd) * 100) if dd else 0}
          for n, lo, hi in day_buckets]
    losers = sorted([s for s in costed if s["profit"] < 0], key=lambda s: s["profit"])[:8]
    return {"bands": bands, "best": best, "best_margin": best_margin, "margins": mh, "days": dh, "losers": losers,
            "costed": len(costed), "total": len(sales), "period_days": days}


GOAL_TYPES = {"profit": "Profit", "revenue": "Revenue", "items_sold": "Items sold", "items_listed": "Items listed",
              "margin": "Margin %"}


def goal_settings():
    raw = Settings().get("goal")
    g = {"type": "profit", "period": "month", "amount": 1000.0}
    if raw:
        try:
            g.update(json.loads(raw))
        except ValueError:
            pass
    return g


def goal_plan(conn):
    g = goal_settings()
    h = store_health(conn)
    since = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = profit.compute_sales(conn, "s.sold_at >= ? AND COALESCE(s.status, '') <> 'cancelled'", (since,))
    rev = sum(s["gross"] for s in recent)
    costed = [s for s in recent if s["profit"] is not None]
    margin = (sum(s["profit"] for s in costed) / sum(s["gross"] for s in costed)) if costed and sum(
        s["gross"] for s in costed) else 0.3
    units = sum(s["quantity"] or 1 for s in recent)
    avg_sale = (rev / units) if units else 20.0
    str_ = (h["metrics"][0]["value"] or 40.0) / 100
    per_month = g["amount"] / (12 if g["period"] == "year" else 1)
    t = g["type"]
    if t == "margin":
        monthly = None
    else:
        if t == "profit":
            revenue = per_month / margin if margin > 0 else None
        elif t == "revenue":
            revenue = per_month
        elif t == "items_sold":
            revenue = per_month * avg_sale
        else:  # items_listed
            revenue = per_month * str_ * avg_sale
        items_sold = (revenue / avg_sale) if revenue else None
        listings = (items_sold / str_) if items_sold and str_ else None
        monthly = {"profit": revenue * margin if revenue else None, "revenue": revenue, "items_sold": items_sold,
                   "items_listed": listings}
    # progress
    today = date.today()
    periods = {"today": today, "week": today - timedelta(days=today.weekday()), "month": today.replace(day=1)}
    import calendar
    dim = calendar.monthrange(today.year, today.month)[1]
    scale = {"today": 1 / dim, "week": 7 / dim, "month": 1.0}
    progress = {}
    for k, s in periods.items():
        ss = profit.compute_sales(conn, "s.sold_at >= ? AND COALESCE(s.status, '') <> 'cancelled'", (s.isoformat(),))
        c = [x for x in ss if x["profit"] is not None]
        actual = {
            "profit": sum(x["profit"] for x in c), "revenue": sum(x["gross"] for x in ss),
            "items_sold": sum(x["quantity"] or 1 for x in ss),
            "items_listed": conn.execute("SELECT COUNT(*) FROM listings WHERE relisted_from IS NULL AND start_time >= ?",
                                         (s.isoformat(),)).fetchone()[0],
            "margin": (sum(x["profit"] for x in c) / sum(x["gross"] for x in c) * 100) if c and sum(
                x["gross"] for x in c) else None,
        }
        target = g["amount"] if t == "margin" else (monthly[t] * scale[k] if monthly and monthly.get(t) else None)
        val = actual[t]
        pct = (val / target * 100) if (target and val is not None) else None
        progress[k] = {"actual": val, "target": target, "pct": pct, "all": actual}
    targets = None
    if monthly:
        targets = {name: {"day": (v / dim) if v else None, "week": (v * 7 / dim) if v else None, "month": v}
                   for name, v in monthly.items()}
    return {"goal": g, "targets": targets, "progress": progress, "assumptions": {
        "margin": margin * 100, "avg_sale": avg_sale, "str": str_ * 100}, "types": GOAL_TYPES}


def monthly_recap(conn) -> None:
    """On (or after) the 1st, a notification summing up last month against the goal."""
    today = date.today()
    first = today.replace(day=1)
    last_first = (first - timedelta(days=1)).replace(day=1)
    key = f"recap-{last_first:%Y-%m}"
    if conn.execute("SELECT 1 FROM notifications WHERE key=?", (key,)).fetchone():
        return
    ss = profit.compute_sales(conn, "s.sold_at >= ? AND s.sold_at < ? AND COALESCE(s.status, '') <> 'cancelled'",
                              (last_first.isoformat(), first.isoformat()))
    if not ss:
        return
    c = [x for x in ss if x["profit"] is not None]
    prof = sum(x["profit"] for x in c)
    rev = sum(x["gross"] for x in ss)
    body = f"Revenue £{rev:,.2f} · item profit £{prof:,.2f} · {sum(x['quantity'] or 1 for x in ss)} items sold."
    g = goal_settings()
    if g["type"] in ("profit", "revenue") and g["amount"]:
        target = g["amount"] / (12 if g["period"] == "year" else 1)
        got = prof if g["type"] == "profit" else rev
        body += f" Goal: £{target:,.0f} {g['type']} — {got / target * 100:.0f}% reached."
    with write_lock():
        db.notify(conn, "info", f"{last_first:%B} recap", body, "/performance?tab=goals", key=key)
        conn.commit()


# --------------------------------------------------------------------------- #
# CSV import wizard
# --------------------------------------------------------------------------- #
IMPORT_FIELDS = {
    "sales": [("sold_at", "Date sold", True), ("title", "Item title", True), ("item_price", "Sale price", True),
              ("channel", "Marketplace", False), ("quantity", "Quantity", False),
              ("delivery_charged", "Postage charged to buyer", False), ("fees", "Platform fees", False),
              ("postage_cost", "Postage you paid", False), ("packaging_cost", "Packaging", False),
              ("cost", "Price paid (COGS)", False), ("notes", "Notes", False)],
    "inventory": [("title", "Item title", True), ("quantity", "Quantity", False), ("cost", "Price paid each", False),
                  ("source", "Bought from", False), ("purchased_at", "Date bought", False),
                  ("location", "Stored at", False), ("sku", "SKU / custom label", False),
                  ("bulk_buy", "Bulk buy name", False), ("notes", "Notes", False)],
    "expenses": [("date", "Date", True), ("description", "Description", True), ("amount", "Amount", True),
                 ("category", "Category", False)],
    "bulk": [("name", "Bulk buy name", True), ("total_cost", "Price paid", True), ("purchased_at", "Date bought", False),
             ("extra_costs", "Extra costs", False), ("item_count", "Number of items", False),
             ("source", "Bought from", False), ("location", "Stored at", False), ("notes", "Notes", False)],
}
IMPORT_KINDS = {"sales": "Sold items (other marketplaces / history)", "inventory": "Inventory (listed or not)",
                "expenses": "Business expenses", "bulk": "Bulk purchases"}
ALIASES = {
    "title": ["title", "item", "item title", "name", "product", "description"],
    "quantity": ["quantity", "qty", "units"], "cost": ["cost", "cogs", "price paid", "purchase price", "buy price",
                                                        "unit cost", "cost of goods"],
    "source": ["source", "bought from", "where bought", "supplier", "sourcing location"],
    "purchased_at": ["purchased_at", "purchase date", "date bought", "bought", "bought on", "date purchased"],
    "location": ["location", "storage", "bin", "shelf", "stored", "stored at"], "sku": ["sku", "custom label"],
    "bulk_buy": ["bulk_buy", "bulk buy", "lot", "haul", "batch"], "notes": ["notes", "note", "comments"],
    "sold_at": ["sold_at", "sale date", "date sold", "sold", "date", "sold date"],
    "channel": ["channel", "marketplace", "platform", "site"],
    "item_price": ["item_price", "sale price", "sold price", "price", "sold for", "sale"],
    "delivery_charged": ["delivery_charged", "shipping charged", "postage charged", "p&p", "shipping income"],
    "fees": ["fees", "platform fees", "selling fees", "marketplace fees"],
    "postage_cost": ["postage_cost", "postage cost", "shipping cost", "postage paid", "shipping"],
    "packaging_cost": ["packaging_cost", "packaging"], "date": ["date", "expense date", "paid on"],
    "description": ["description", "details", "item", "expense", "what"], "amount": ["amount", "total", "cost", "value"],
    "category": ["category", "type"], "name": ["name", "bulk buy", "haul", "lot", "title"],
    "total_cost": ["total_cost", "price paid", "total", "cost", "amount"], "extra_costs": ["extra_costs", "extras", "fees"],
    "item_count": ["item_count", "items", "number of items", "count", "quantity"],
}


def _suggest(header, kind):
    """Guess which column holds each field: exact names first, then headings containing a known name
    (e.g. "Purchase Date" -> date, "Total (£)" -> amount)."""
    norm = [re.sub(r"[^a-z0-9& ]+", " ", h.lower()).strip() for h in header]
    out = {}
    used = set()
    for exact in (True, False):
        for f, _, _ in IMPORT_FIELDS[kind]:
            if f in out:
                continue
            names = ALIASES.get(f, [f])
            for i, h in enumerate(norm):
                if i in used or not h:
                    continue
                hit = h in names if exact else any(re.search(rf"\b{re.escape(n)}\b", h) for n in names if len(n) > 2)
                if hit:
                    out[f] = i
                    used.add(i)
                    break
    return out


def _date(v):
    v = (v or "").strip()
    if len(v) >= 10 and v[4:5] == "-":
        v = v[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y", "%d.%m.%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _category(v):
    v = (v or "").lower()
    for k, label, _ in taxcalc.EXPENSE_CATEGORIES:
        if k in v or any(w in v for w in label.lower().replace(",", "").split() if len(w) > 4):
            return k
    return "other"


def _import_dir():
    d = db.config_dir() / "imports"
    d.mkdir(exist_ok=True)
    for f in d.glob("*.csv"):
        if time.time() - f.stat().st_mtime > 86400:
            f.unlink(missing_ok=True)
    return d


def run_import(conn, kind, rows, mapping):
    def g(r, k):
        i = mapping.get(k)
        return r[i].strip() if i is not None and i < len(r) else ""
    added = skipped = 0
    with write_lock():
        bulk_ids = {r["name"].lower(): r["id"] for r in conn.execute("SELECT id, name FROM bulk_buys")}
        for r in rows:
            req = [f for f, _, need in IMPORT_FIELDS[kind] if need]
            if not any(c.strip() for c in r) or any(not g(r, f) for f in req):
                skipped += 1
                continue
            if kind == "inventory":
                bname, bid = g(r, "bulk_buy"), None
                if bname:
                    bid = bulk_ids.get(bname.lower())
                    if bid is None:
                        bid = conn.execute("INSERT INTO bulk_buys(name, total_cost, created_at) VALUES (?,0,?)",
                                           (bname, now_iso())).lastrowid
                        bulk_ids[bname.lower()] = bid
                cur = conn.execute("""INSERT INTO items(title, sku, quantity, cost, source, purchased_at, location,
                        bulk_buy_id, notes, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                                   (g(r, "title"), g(r, "sku") or None, parse_int(g(r, "quantity")) or 1,
                                    parse_money(g(r, "cost")), g(r, "source") or None, _date(g(r, "purchased_at")),
                                    g(r, "location") or None, bid, g(r, "notes") or None, now_iso(), now_iso()))
                add_event(conn, cur.lastrowid, "created", "Imported from spreadsheet")
            elif kind == "sales":
                sold = _date(g(r, "sold_at"))
                if not sold:
                    skipped += 1
                    continue
                lid = "M-" + uuid.uuid4().hex[:12]
                conn.execute("""INSERT INTO sales(line_item_id, order_id, title, quantity, item_price, delivery_charged,
                        manual_fees, postage_cost, packaging_cost, cost, channel, sold_at, notes, source, currency,
                        updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'manual', 'GBP', ?)""",
                             (lid, lid, g(r, "title"), parse_int(g(r, "quantity")) or 1,
                              parse_money(g(r, "item_price")) or 0, parse_money(g(r, "delivery_charged")) or 0,
                              parse_money(g(r, "fees")) or 0, parse_money(g(r, "postage_cost")),
                              parse_money(g(r, "packaging_cost")), parse_money(g(r, "cost")),
                              g(r, "channel") or "Other", sold + "T12:00:00Z", g(r, "notes") or None, now_iso()))
            elif kind == "expenses":
                d = _date(g(r, "date"))
                if not d:
                    skipped += 1
                    continue
                conn.execute("INSERT INTO expenses(date, category, description, amount, created_at) VALUES (?,?,?,?,?)",
                             (d, _category(g(r, "category")), g(r, "description"),
                              parse_money(g(r, "amount")) or 0, now_iso()))
            else:
                conn.execute("""INSERT INTO bulk_buys(name, purchased_at, total_cost, extra_costs, item_count, notes,
                        created_at, source, location) VALUES (?,?,?,?,?,?,?,?,?)""",
                             (g(r, "name"), _date(g(r, "purchased_at")), parse_money(g(r, "total_cost")) or 0,
                              parse_money(g(r, "extra_costs")) or 0, parse_int(g(r, "item_count")),
                              g(r, "notes") or None, now_iso(), g(r, "source") or None, g(r, "location") or None))
            added += 1
        conn.commit()
    return added, skipped


# --------------------------------------------------------------------------- #
# Receipts (stored next to the database so they sync with it)
# --------------------------------------------------------------------------- #
RECEIPT_EXT = {".pdf", ".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif"}


def receipts_dir() -> Path:
    d = Path(db.get_db_path()).parent / "Profit Tracker receipts"
    d.mkdir(exist_ok=True)
    return d


def save_receipt(file, expense_id, when):
    if not file or not file.filename:
        return None
    ext = Path(file.filename).suffix.lower()
    if ext not in RECEIPT_EXT:
        raise ValueError("Receipts can be PDF or photos (jpg, png, heic, webp).")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(file.filename).stem)[:40]
    sub = receipts_dir() / (when or date.today().isoformat())[:4]
    sub.mkdir(exist_ok=True)
    path = sub / f"{expense_id}-{safe}{ext}"
    file.save(path)
    if path.stat().st_size > 15 * 1024 * 1024:
        path.unlink()
        raise ValueError("That receipt is over 15 MB.")
    return str(path.relative_to(receipts_dir().parent))


# --------------------------------------------------------------------------- #
# Phone access (LAN mode + one-time pairing link)
# --------------------------------------------------------------------------- #
PAIR = {"token": None, "expires": 0}


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.0.1", 9))   # no packets are sent; this just picks the LAN interface
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


def register(app):  # noqa: C901

    @app.context_processor
    def _fy_ctx():
        return {"fy_label": fy_label}

    # ======================================================================= #
    # P&L
    # ======================================================================= #
    @app.route("/pnl")
    @login_required
    def pnl_page():
        start, end, label, mode, year, q = pnl_period(request.args)
        conn = connect()
        try:
            r = pnl(conn, start, end)
            stock = profit.stock_on_hand(conn)
            first = conn.execute("SELECT MIN(sold_at) FROM sales").fetchone()[0]
        finally:
            conn.close()
        cur = fy_of()
        years = list(range(cur, min(fy_of(first[:10]) if first else cur, year) - 1, -1))
        accountant = Settings().get("accountant_email") or ""
        lines = [f"{label}", f"Revenue: £{r['revenue']:,.2f}", f"Cost of goods sold: £{r['cogs']:,.2f}",
                 f"Selling fees: £{r['fee_total']:,.2f}", f"Promoted listings: £{r['promoted']:,.2f}",
                 f"Shipping & packaging: £{r['shipping']:,.2f}", f"Handling (labour): £{r['labour']:,.2f}",
                 f"Item profit: £{r['item_profit']:,.2f}",
                 f"Business expenses: £{r['expense_total']:,.2f}", f"Write-offs: £{r['write_off_total']:,.2f}",
                 f"Net profit (before income tax): £{r['net']:,.2f}", "",
                 "The full statement (CSV) is attached."]
        mailto = (f"mailto:{quote(accountant)}?subject={quote('Profit & loss — ' + label)}"
                  f"&body={quote(chr(10).join(lines))}")
        return render_template("pnl.html", r=r, label=label, mode=mode, year=year, q=q, years=years, stock=stock,
                               mailto=mailto, accountant=accountant, args=request.args)

    @app.route("/pnl.csv")
    @login_required
    def pnl_csv():
        start, end, label, *_ = pnl_period(request.args)
        conn = connect()
        try:
            r = pnl(conn, start, end)
            sales = profit.compute_sales(conn, "s.sold_at >= ? AND s.sold_at < ?", (start, end))
        finally:
            conn.close()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([f"Profit and loss — {label}"])
        w.writerow(["Line", "Amount (£)"])
        rows = [("Item sales", r["item_sales"]), ("Postage charged", r["postage_in"]), ("Refunds", -r["refunds"]),
                ("Revenue", r["revenue"]), ("Cost of goods sold", -r["cogs"])]
        rows += [(k, -v) for k, v in r["fees"].items()]
        rows += [("Promoted listing fees", -r["promoted"]), ("Shipping & packaging", -r["shipping"]),
                 ("Other sale costs", -r["other"]),
                 ("Handling (labour)" + ("" if r["labour_paid"] else " - your own time, not a tax expense"), -r["labour"]),
                 ("ITEM PROFIT", r["item_profit"])]
        rows += [(f"{k} (eBay account)", -v) for k, v in r["account"]["items"].items()]
        rows += [(k, -v) for k, v in r["expenses"].items()]
        rows += [(f"Write-off: {w_['title']} ({w_['write_off_reason']})", -w_["value"]) for w_ in r["write_offs"]]
        rows += [("NET PROFIT (before income tax)", r["net"])]
        for row in rows:
            w.writerow(row)
        w.writerow([])
        w.writerow(["Date", "Channel", "Title", "Qty", "Buyer paid", "Fees & charges", "Cost", "Item profit"])
        for s in sorted(sales, key=lambda s: s["sold_at"] or ""):
            w.writerow([(s["sold_at"] or "")[:10], s.get("channel") or "eBay", s["title"], s["quantity"], s["gross"],
                        _r(s["ebay_deductions_total"] + s["own_costs_total"]),
                        "" if s["item_cost"] is None else s["item_cost"], "" if s["profit"] is None else s["profit"]])
        fname = re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_")
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=PnL_{fname}.csv"})

    # ======================================================================= #
    # Receipts
    # ======================================================================= #
    @app.route("/receipts/<int:expense_id>")
    @login_required
    def receipt(expense_id):
        conn = connect()
        try:
            r = conn.execute("SELECT receipt_path FROM expenses WHERE id=?", (expense_id,)).fetchone()
        finally:
            conn.close()
        if not r or not r["receipt_path"]:
            abort(404)
        path = (Path(db.get_db_path()).parent / r["receipt_path"]).resolve()
        base = receipts_dir().resolve()
        if base not in path.parents or not path.exists():
            abort(404)
        return send_file(path, mimetype=mimetypes.guess_type(str(path))[0] or "application/octet-stream")

    # ======================================================================= #
    # Import wizard
    # ======================================================================= #
    @app.route("/import", methods=["GET", "POST"])
    @login_required
    def import_page():
        if request.method == "GET":
            return render_template("import.html", kinds=IMPORT_KINDS, fields=IMPORT_FIELDS)
        kind = request.form.get("kind")
        f = request.files.get("file")
        if kind not in IMPORT_FIELDS or not f or not f.filename:
            flash("Choose what you're importing and a CSV file.", "error")
            return redirect(url_for("import_page"))
        raw = f.read()
        if len(raw) > 20 * 1024 * 1024:
            flash("That file is over 20 MB.", "error")
            return redirect(url_for("import_page"))
        token = secrets.token_hex(8)
        (_import_dir() / f"{token}.csv").write_bytes(raw)
        return redirect(url_for("import_map", token=token, kind=kind))

    @app.route("/import/map/<token>", methods=["GET", "POST"])
    @login_required
    def import_map(token):
        if not re.fullmatch(r"[0-9a-f]{16}", token):
            abort(404)
        path = _import_dir() / f"{token}.csv"
        if not path.exists():
            flash("That upload has expired. Please upload it again.", "error")
            return redirect(url_for("import_page"))
        kind = request.values.get("kind")
        if kind not in IMPORT_FIELDS:
            abort(400)
        text = path.read_bytes().decode("utf-8-sig", errors="replace")
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        rows = [r for r in csv.reader(io.StringIO(text), dialect)]
        if len(rows) < 2:
            flash("That file has no data rows.", "error")
            return redirect(url_for("import_page"))
        header, data = rows[0], rows[1:]
        if request.method == "POST":
            mapping = {f: parse_int(request.form.get(f"map_{f}")) for f, _, _ in IMPORT_FIELDS[kind]}
            mapping = {k: v for k, v in mapping.items() if v is not None and v >= 0}
            missing = [lbl for f, lbl, need in IMPORT_FIELDS[kind] if need and f not in mapping]
            if missing:
                flash("Please choose a column for: " + ", ".join(missing), "error")
                return redirect(url_for("import_map", token=token, kind=kind))
            conn = connect()
            try:
                added, skipped = run_import(conn, kind, data, mapping)
            finally:
                conn.close()
            path.unlink(missing_ok=True)
            flash(f"Imported {added} row{'s' if added != 1 else ''}"
                  + (f", skipped {skipped} (empty, or missing a required value/date)" if skipped else "") + ".", "ok")
            return redirect({"sales": url_for("sales_list"), "inventory": url_for("inventory", status="all"),
                             "expenses": url_for("expenses"), "bulk": url_for("bulk_list")}[kind])
        return render_template("import_map.html", kind=kind, kind_label=IMPORT_KINDS[kind], header=header,
                               preview=data[:5], total=len(data), fields=IMPORT_FIELDS[kind],
                               suggested=_suggest(header, kind), token=token)

    # ======================================================================= #
    # Performance: store health, stale stock, Growth Lab, goals
    # ======================================================================= #
    @app.route("/performance", methods=["GET", "POST"])
    @login_required
    def performance():
        tab = request.args.get("tab", "health")
        st = Settings()
        if request.method == "POST":
            if request.form.get("form") == "targets":
                t = health_targets()
                for k in ("str", "asp", "days", "stale"):
                    v = parse_float(request.form.get(k))
                    if v is not None:
                        t[k] = max(v, 0)
                t["stale_days"] = max(parse_int(request.form.get("stale_days")) or t["stale_days"], 7)
                st.set("health_targets", json.dumps(t))
                flash("Targets saved.", "ok")
            else:
                g = goal_settings()
                g["type"] = request.form.get("type") if request.form.get("type") in GOAL_TYPES else "profit"
                g["period"] = "year" if request.form.get("period") == "year" else "month"
                g["amount"] = max(parse_float(request.form.get("amount")) or 0, 0)
                st.set("goal", json.dumps(g))
                flash("Goal saved.", "ok")
            return redirect(url_for("performance", tab=tab))
        conn = connect()
        try:
            health = store_health(conn)
            lab = growth_lab(conn) if tab == "growth" else None
            plan = goal_plan(conn) if tab == "goals" else None
            monthly_recap(conn)
        finally:
            conn.close()
        return render_template("performance.html", tab=tab, h=health, lab=lab, plan=plan)

    # ======================================================================= #
    # Calculators
    # ======================================================================= #
    @app.route("/tools")
    @login_required
    def tools():
        conn = connect()
        try:
            since = (datetime.now(timezone.utc) - timedelta(days=180)).strftime("%Y-%m-%dT%H:%M:%SZ")
            sales = profit.compute_sales(conn, "s.sold_at >= ? AND s.source='ebay'", (since,))
            sold90 = conn.execute("SELECT COALESCE(SUM(quantity),0) FROM sales WHERE sold_at >= ?",
                                  ((datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d"),)).fetchone()[0]
            active = conn.execute("SELECT COUNT(*) FROM listings WHERE status='active'").fetchone()[0]
            defaults = profit.packing_defaults(conn)
        finally:
            conn.close()
        fee_lines = {"Final value fee", "Regulatory operating fee", "International fee", "eBay selling fees"}
        base = sum(s["gross"] for s in sales)
        fees = sum(v for s in sales for k, v in s["ebay_deductions"].items() if k in fee_lines)
        promo = sum(s["ebay_deductions"].get("Promoted listings", 0) for s in sales)
        your_fee = (fees / base * 100) if base else None
        your_promo = (promo / base * 100) if base else None
        return render_template("tools.html", your_fee=your_fee, your_promo=your_promo, sold90=sold90, active=active,
                               defaults=defaults)

    # ======================================================================= #
    # Phone access
    # ======================================================================= #
    @app.route("/phone", methods=["GET", "POST"])
    @login_required
    def phone():
        if request.method == "POST":
            PAIR.update(token=secrets.token_urlsafe(16), expires=time.time() + 600)
            return redirect(url_for("phone"))
        ip = lan_ip()
        link = None
        if app.config.get("LAN") and ip and PAIR["token"] and PAIR["expires"] > time.time():
            link = f"{app.config['SCHEME']}://{ip}:{app.config['PORT']}/pair/{PAIR['token']}"
        return render_template("phone.html", lan=app.config.get("LAN"), ip=ip, link=link,
                               server_url=app.config.get("PUBLIC_URL"),
                               expires=int(max(PAIR["expires"] - time.time(), 0) / 60))

    @app.route("/pair/<token>")
    def pair(token):
        if not (PAIR["token"] and secrets.compare_digest(token, PAIR["token"]) and PAIR["expires"] > time.time()):
            return render_template("pair_failed.html"), 403
        PAIR.update(token=None, expires=0)   # one use only
        st = Settings()
        session.clear()
        session.permanent = True
        session.update(user_id=st.get("owner_user_id") or "demo", user_name=st.get("owner_username") or "",
                       db_path=db.get_db_path(), paired=True)
        flash("This phone is now signed in.", "ok")
        return redirect(url_for("dashboard"))

    @app.route("/manifest.webmanifest")
    def manifest():
        return jsonify({
            "name": "Flip for Free", "short_name": "Flip", "start_url": "/", "display": "standalone",
            "background_color": "#2b2f2d", "theme_color": "#3ecf8e",
            "icons": [{"src": url_for("static", filename="icon.svg"), "sizes": "any", "type": "image/svg+xml"},
                      {"src": url_for("static", filename="icon-192.png"), "sizes": "192x192", "type": "image/png"},
                      {"src": url_for("static", filename="icon-512.png"), "sizes": "512x512", "type": "image/png"},
                      {"src": url_for("static", filename="maskable-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "maskable"}]})
