"""Time to sell.

A listing is followed through every relist (new eBay item IDs, linked by `relisted_from`, or the same inventory item),
so "listed" always means the first time it went up, not the latest relist.

* Single-quantity listings: days from first listed to sold. Averaged, with the median and a spread.
* Multi-quantity listings: how often one sells, i.e. days listed ÷ units sold ("one sells every N days"), plus
  sales per week and, for the stock left, roughly how long it will take to sell out at that rate.
"""
from collections import defaultdict
from datetime import datetime, timezone
from statistics import median


def _parse(ts):
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _days(a, b):
    return (b - a).total_seconds() / 86400 if a and b else None


def chains(conn):
    """Group listings into chains: one per thing you listed, across relists. Returns {chain_key: info}."""
    rows = [dict(r) for r in conn.execute(
        """SELECT legacy_item_id, item_id, title, quantity, quantity_available, quantity_sold, start_time,
                  end_time, status, relisted_from, price, url FROM listings""")]
    by_id = {r["legacy_item_id"]: r for r in rows}

    def root(lid, seen=None):
        seen = seen or set()
        r = by_id.get(lid)
        while r and r.get("relisted_from") and r["relisted_from"] in by_id and r["relisted_from"] not in seen:
            seen.add(r["legacy_item_id"])
            r = by_id[r["relisted_from"]]
        return r["legacy_item_id"] if r else lid

    out = {}
    key_of = {}
    for r in rows:
        key = f"item:{r['item_id']}" if r["item_id"] else f"listing:{root(r['legacy_item_id'])}"
        key_of[r["legacy_item_id"]] = key
        c = out.setdefault(key, {"key": key, "item_id": r["item_id"], "title": r["title"], "first_listed": None,
                                 "max_qty": 0, "active": False, "stock_left": 0, "listing_ids": [], "latest": None,
                                 "url": r["url"], "price": r["price"]})
        c["listing_ids"].append(r["legacy_item_id"])
        st = _parse(r["start_time"])
        if st and (c["first_listed"] is None or st < c["first_listed"]):
            c["first_listed"] = st
        c["max_qty"] = max(c["max_qty"], r["quantity"] or 1, (r["quantity_sold"] or 0) + (r["quantity_available"] or 0))
        if r["status"] == "active":
            c["active"] = True
            c["stock_left"] += r["quantity_available"] or 0
        if st and (c["latest"] is None or st >= c["latest"][0]):
            c["latest"] = (st, r["title"], r["url"], r["price"])
    for c in out.values():
        if c["latest"]:
            _, c["title"], c["url"], c["price"] = c["latest"]
        c.pop("latest")
    return out, key_of


def compute(conn, start=None, end=None, now=None):
    """Time-to-sell figures. `start`/`end` (ISO dates) limit which sales count; listings are followed all time."""
    now = now or datetime.now(timezone.utc)
    ch, key_of = chains(conn)
    sales = defaultdict(list)                     # chain key -> [(sold_at, units)]
    for s in conn.execute("SELECT legacy_item_id, sold_at, quantity FROM sales WHERE legacy_item_id IS NOT NULL "
                          "AND sold_at IS NOT NULL AND COALESCE(status, '') NOT IN ('cancelled', 'refunded')"):
        k = key_of.get(s["legacy_item_id"])
        if k:
            sales[k].append((_parse(s["sold_at"]), s["quantity"] or 1))
    s_lo, s_hi = _parse(start + "T00:00:00Z") if start else None, _parse(end + "T00:00:00Z") if end else None
    in_period = lambda d: d and (not s_lo or d >= s_lo) and (not s_hi or d < s_hi)  # noqa: E731

    single_days, single_rows, still_listed, multi_rows = [], [], [], []
    for k, c in ch.items():
        fl = c["first_listed"]
        if not fl:
            continue
        sold = sorted((d, q) for d, q in sales.get(k, []) if d)
        units_all = sum(q for _, q in sold)
        multi = c["max_qty"] > 1 or units_all > 1
        if not multi:
            if sold:
                d = _days(fl, sold[0][0])
                if d is not None and d >= 0 and in_period(sold[0][0]):
                    single_days.append(d)
                    single_rows.append({"title": c["title"], "days": d, "sold_at": sold[0][0], "url": c["url"],
                                        "item_id": c["item_id"]})
            elif c["active"]:
                still_listed.append({"title": c["title"], "days": _days(fl, now), "url": c["url"],
                                     "item_id": c["item_id"], "price": c["price"]})
            continue
        # multi-quantity: rate over the days it was listed (to the last sale if it has ended, or to now)
        period_sales = [(d, q) for d, q in sold if in_period(d)]
        units = sum(q for _, q in period_sales)
        win_lo = max(fl, s_lo) if s_lo else fl
        win_hi = now if c["active"] else (sold[-1][0] if sold else now)
        if s_hi:
            win_hi = min(win_hi, s_hi)
        listed_days = max(_days(win_lo, win_hi) or 0, 1)
        if not units and not c["active"]:
            continue
        every = (listed_days / units) if units else None
        multi_rows.append({
            "title": c["title"], "url": c["url"], "item_id": c["item_id"], "units": units, "listed_days": listed_days,
            "every_days": every, "per_week": units / listed_days * 7, "stock_left": c["stock_left"],
            "active": c["active"], "last_sale": sold[-1][0] if sold else None,
            "days_since_sale": _days(sold[-1][0], now) if sold else _days(fl, now),
            "sell_out_days": (c["stock_left"] * every) if (every and c["stock_left"] and c["active"]) else None})

    def spread(days):
        buckets = [("Within a week", 0, 7), ("1–4 weeks", 7, 28), ("1–3 months", 28, 91), ("Over 3 months", 91, 10**6)]
        n = len(days) or 1
        return [{"label": lab, "count": sum(lo <= d < hi for d in days),
                 "pct": sum(lo <= d < hi for d in days) / n * 100} for lab, lo, hi in buckets]

    multi_rows.sort(key=lambda r: (r["every_days"] is None, r["every_days"] or 0))
    still_listed.sort(key=lambda r: -(r["days"] or 0))
    rated = [r for r in multi_rows if r["every_days"]]
    total_units = sum(r["units"] for r in multi_rows)
    total_days = sum(r["listed_days"] for r in rated)
    return {
        "single": {
            "count": len(single_days),
            "avg": sum(single_days) / len(single_days) if single_days else None,
            "median": median(single_days) if single_days else None,
            "fastest": min(single_days) if single_days else None,
            "slowest": max(single_days) if single_days else None,
            "spread": spread(single_days),
            "recent": sorted(single_rows, key=lambda r: r["sold_at"], reverse=True)[:10],
            "still_listed": still_listed[:10], "still_listed_count": len(still_listed),
            "still_listed_avg": (sum(r["days"] for r in still_listed) / len(still_listed)) if still_listed else None,
            "all_sold": single_rows, "all_listed": still_listed,
        },
        "multi": {
            "listings": multi_rows, "count": len(multi_rows), "units": total_units,
            # across all multi-quantity listings: listing-days per unit sold
            "every_days": (sum(r["listed_days"] for r in rated) / sum(r["units"] for r in rated)) if rated else None,
            "median_every": median([r["every_days"] for r in rated]) if rated else None,
            "per_week": (total_units / total_days * 7) if total_days else None,
        },
    }


def for_item(conn, item_id, now=None):
    """Time to sell for one inventory item (its listings across relists)."""
    now = now or datetime.now(timezone.utc)
    t = compute(conn, now=now)
    for r in t["multi"]["listings"]:
        if r["item_id"] == item_id:
            return {"kind": "multi", **r}
    for r in t["single"]["all_sold"] + t["single"]["all_listed"]:
        if r["item_id"] == item_id:
            return {"kind": "single", **r, "sold": "sold_at" in r}
    return None
