"""Turns raw orders + eBay money movements into a true profit figure.

For each order:
    net received from eBay = sum of every money movement linked to it
                             (sale payout, refunds, postage labels, ad fees, disputes...)
    eBay deductions         = what the buyer paid you - net received
The deductions are broken down by type, and any part eBay doesn't explain is shown
as "Other eBay adjustments" so the numbers always reconcile.

Then for each sale (order line):
    money made   = net received - your own costs (postage you paid elsewhere, packaging, other)
    profit       = money made - what you paid for the item
Bulk buys: the lot's total cost (+ extras) is split across its items. Items with a
locked cost keep it; the remainder is divided evenly over the other units. Each sale
of a bulk item therefore gets an allocated cost, and the bulk buy page also shows
the lot as a whole (money made - total cost).
"""
import json
from collections import defaultdict

EXCLUDED_TYPES = {"TRANSFER", "PAYOUT", "WITHDRAWAL", "LOAN_REPAYMENT", "PURCHASE"}

FEE_LABELS = {
    "FINAL_VALUE_FEE": "Final value fee",
    "FINAL_VALUE_FEE_FIXED_PER_ORDER": "Per-order fee",
    "REGULATORY_OPERATING_FEE": "Regulatory operating fee",
    "INTERNATIONAL_FEE": "International fee",
    "AD_FEE": "Promoted listings",
    "BELOW_STANDARD_SURCHARGE": "Below-standard surcharge",
    "HIGH_ITEM_NOT_AS_DESCRIBED_FEE": "Not-as-described fee",
    "INSERTION_FEE": "Insertion fees",
    "SUBSCRIPTION_FEE": "Shop subscription",
    "PAYMENT_DISPUTE_FEE": "Dispute fees",
}
TYPE_LABELS = {
    "SHIPPING_LABEL": "eBay postage labels",
    "REFUND": "Refunds",
    "DISPUTE": "Disputes",
    "CREDIT": "eBay credits",
    "ADJUSTMENT": "Adjustments",
}
OTHER_LABEL = "Other eBay adjustments"
ESTIMATE_LABEL = "eBay fees (estimated)"


def fee_label(fee_type: str | None) -> str:
    if not fee_type:
        return "Other fees"
    return FEE_LABELS.get(fee_type, fee_type.replace("_", " ").capitalize())


def tx_label(t) -> str:
    if t["type"] == "NON_SALE_CHARGE":
        return fee_label(t["fee_type"])
    return TYPE_LABELS.get(t["type"], t["type"].replace("_", " ").capitalize())


def signed(t) -> float:
    amt = t["amount"] or 0.0
    return amt if t["booking"] == "CREDIT" else -amt


def _r(x):
    return round(x + 0.0, 2)


def _item_prices(conn) -> dict:
    """Best guess at each item's selling price: average sold unit price, else current/last list price."""
    out = {}
    for r in conn.execute("""SELECT item_id, AVG(item_price / MAX(COALESCE(quantity, 1), 1)) p FROM sales
                             WHERE item_id IS NOT NULL AND item_price > 0 GROUP BY item_id"""):
        out[r["item_id"]] = r["p"]
    for r in conn.execute("""SELECT item_id, price FROM listings WHERE item_id IS NOT NULL AND price > 0
                             ORDER BY (status = 'active'), start_time"""):
        out.setdefault(r["item_id"], r["price"])
    return out


def _item_values(conn) -> dict:
    """A price for each item, used to weight bulk-buy cost splits: its current listing price,
    or the average unit price it sold for."""
    vals = {}
    for r in conn.execute("""SELECT item_id, price FROM listings WHERE item_id IS NOT NULL AND price IS NOT NULL
                             ORDER BY start_time"""):
        vals[r["item_id"]] = r["price"]
    for r in conn.execute("""SELECT item_id, SUM(item_price) / SUM(COALESCE(quantity, 1)) AS unit FROM sales
                             WHERE item_id IS NOT NULL GROUP BY item_id"""):
        vals.setdefault(r["item_id"], r["unit"])
    return vals


def bulk_allocations(conn) -> dict:
    """{bulk_id: {"per_unit": avg unit cost of unlocked items|None, "items": {item_id: unit cost},
                  "units": int, "locked_total": float, "cost": float, "mode": "even"|"price"}}

    even  - the cost left after fixed (locked) items is divided equally per unit.
    price - it is split in proportion to each item's price (listing price or sold price);
            items with no price yet are treated as the average-priced item.
    """
    out = {}
    items_by_bulk = defaultdict(list)
    for it in conn.execute("SELECT id, bulk_buy_id, quantity, cost, cost_locked FROM items "
                           "WHERE bulk_buy_id IS NOT NULL"):
        items_by_bulk[it["bulk_buy_id"]].append(it)
    values = None
    for b in conn.execute("SELECT id, total_cost, extra_costs, item_count, split_method FROM bulk_buys"):
        cost = (b["total_cost"] or 0) + (b["extra_costs"] or 0)
        its = items_by_bulk.get(b["id"], [])
        locked = [i for i in its if i["cost_locked"] and i["cost"] is not None]
        free = [i for i in its if not (i["cost_locked"] and i["cost"] is not None)]
        locked_total = sum(i["cost"] * (i["quantity"] or 1) for i in locked)
        locked_units = sum(i["quantity"] or 1 for i in locked)
        logged_units = sum(i["quantity"] or 1 for i in its)
        units = max(b["item_count"] or 0, logged_units)
        free_units = units - locked_units
        remaining = max(cost - locked_total, 0.0)
        per_unit = remaining / free_units if free_units > 0 else None
        mode = b["split_method"] or "even"
        item_costs = {i["id"]: i["cost"] for i in locked}
        if mode == "price" and free:
            if values is None:
                values = _item_values(conn)
            known = [values[i["id"]] for i in free if values.get(i["id"])]
            avg = (sum(known) / len(known)) if known else 1.0
            # units in the lot that haven't been logged as items count at the average price
            unlogged = max(units - logged_units, 0)
            weight_total = sum((values.get(i["id"]) or avg) * (i["quantity"] or 1) for i in free) + unlogged * avg
            for i in free:
                w = values.get(i["id"]) or avg
                item_costs[i["id"]] = remaining * w / weight_total if weight_total else per_unit
        else:
            for i in free:
                item_costs[i["id"]] = per_unit
        out[b["id"]] = {"per_unit": per_unit, "items": item_costs, "units": units,
                        "locked_total": locked_total, "cost": cost, "mode": mode, "method": mode}
    return out


def item_unit_costs(conn, alloc=None) -> dict:
    """{item_id: unit cost or None}, with bulk items taking their share of the lot."""
    alloc = alloc if alloc is not None else bulk_allocations(conn)
    out = {}
    for it in conn.execute("SELECT id, bulk_buy_id, cost, cost_locked FROM items"):
        if it["bulk_buy_id"]:
            out[it["id"]] = (alloc.get(it["bulk_buy_id"]) or {}).get("items", {}).get(it["id"])
        else:
            out[it["id"]] = it["cost"]
    return out


PACKAGING = "Packaging"
LABOUR = "Handling (labour)"
NO_PARCEL_CHANNELS = {"Car boot", "In person"}


def packing_defaults(conn) -> dict:
    """Default packaging and handling (labour) per sale, from Settings. They only apply to sales on or
    after `from` so setting them doesn't rewrite your history."""
    rows = {r["key"]: r["value"] for r in conn.execute(
        "SELECT key, value FROM settings WHERE key IN ('default_packaging', 'default_handling', 'packing_defaults_from')")}

    def num(k):
        try:
            v = float(rows.get(k) or 0)
            return v if v > 0 else None
        except ValueError:
            return None
    return {"packaging": num("default_packaging"), "handling": num("default_handling"),
            "from": rows.get("packing_defaults_from") or "9999"}


def _pick(sale_value, item_value, default, s, d):
    """Sale override > listing (item) amount > default. Returns (amount, where it came from)."""
    if sale_value is not None:
        return sale_value, "sale"
    if item_value is not None:
        return item_value * 1, "listing"
    if default and (s.get("sold_at") or "") >= d["from"] and s.get("status") != "cancelled":
        if s.get("source") == "manual" and (s.get("channel") in NO_PARCEL_CHANNELS):
            return None, None
        return default, "default"
    return None, None


def compute_sales(conn, where: str = "1=1", params: tuple = ()) -> list[dict]:
    """Return enriched sale dicts for sales matching WHERE (on alias s)."""
    sales = [dict(r) for r in conn.execute(
        f"""SELECT s.*, b.name AS bulk_name, i.location AS item_location, i.source AS item_source,
                   i.purchased_at AS item_purchased_at, i.packaging_cost AS item_packaging,
                   i.handling_cost AS item_handling
            FROM sales s
            LEFT JOIN bulk_buys b ON b.id = s.bulk_buy_id
            LEFT JOIN items i ON i.id = s.item_id
            WHERE {where} ORDER BY s.sold_at DESC""", params)]
    if not sales:
        return []
    order_ids = sorted({s["order_id"] for s in sales if s["order_id"]})

    # every line of those orders (needed to split order-level charges)
    order_lines = defaultdict(list)
    orders = {}
    txs = defaultdict(list)
    for chunk in _chunks(order_ids, 400):
        ph = ",".join("?" * len(chunk))
        for r in conn.execute(f"SELECT * FROM sales WHERE order_id IN ({ph})", chunk):
            order_lines[r["order_id"]].append(dict(r))
        for r in conn.execute(f"SELECT * FROM orders WHERE order_id IN ({ph})", chunk):
            orders[r["order_id"]] = dict(r)
        for r in conn.execute(f"SELECT * FROM transactions WHERE order_id IN ({ph})", chunk):
            if r["type"] not in EXCLUDED_TYPES:
                txs[r["order_id"]].append(dict(r))

    breakdowns = {}
    for oid in order_ids:
        if oid.startswith(("M-", "CT-", "SH-")):     # manual, CardTrader and Shopify sales carry their own fees
            continue
        breakdowns.update(_order_breakdown(oid, order_lines[oid], orders.get(oid), txs[oid]))
    alloc = bulk_allocations(conn)
    unit_costs = item_unit_costs(conn, alloc)
    pack_defaults = packing_defaults(conn)

    out = []
    for s in sales:
        gross = (s["item_price"] or 0) + (s["delivery_charged"] or 0)
        bd = breakdowns.get(s["line_item_id"])
        if bd is None:
            fees = s.get("manual_fees") or 0.0
            bd = {"deductions": {f"{s.get('channel') or 'Platform'} fees": _r(fees)} if fees else {},
                  "net": _r(gross - fees), "estimated": False}
        ded = dict(bd["deductions"])
        ebay_total = sum(ded.values())
        own = {}
        if s["postage_cost"]:
            own["Postage (paid by you)"] = s["postage_cost"]
        packaging, packaging_from = _pick(s["packaging_cost"], s.get("item_packaging"),
                                          pack_defaults["packaging"], s, pack_defaults)
        handling, handling_from = _pick(s.get("handling_cost"), s.get("item_handling"),
                                        pack_defaults["handling"], s, pack_defaults)
        if packaging:
            own[PACKAGING] = packaging
        if handling:
            own[LABOUR] = handling
        if s["other_cost"]:
            own["Other costs"] = s["other_cost"]
        own_total = sum(own.values())
        money_made = bd["net"] - own_total
        in_bulk = s["bulk_buy_id"] is not None
        qty = s["quantity"] or 1
        cost_source = None
        item_cost = None
        if s["cost"] is not None and not in_bulk:
            item_cost, cost_source = s["cost"], "sale"
        elif s.get("item_id") and unit_costs.get(s["item_id"]) is not None:
            item_cost = unit_costs[s["item_id"]] * qty
            cost_source = "bulk share" if in_bulk else "item"
        elif in_bulk and (alloc.get(s["bulk_buy_id"]) or {}).get("per_unit") is not None:
            item_cost, cost_source = alloc[s["bulk_buy_id"]]["per_unit"] * qty, "bulk share"
        profit = None if item_cost is None else money_made - item_cost
        s.update(
            gross=_r(gross),
            ebay_deductions=ded,
            ebay_deductions_total=_r(ebay_total),
            net_from_ebay=_r(bd["net"]),
            own_costs=own,
            own_costs_total=_r(own_total),
            money_made=_r(money_made),
            item_cost=None if item_cost is None else _r(item_cost),
            cost_source=cost_source,
            packaging=packaging or 0.0, packaging_from=packaging_from,
            handling=handling or 0.0, handling_from=handling_from,
            profit=None if profit is None else _r(profit),
            estimated=bd["estimated"],
            in_bulk=in_bulk,
            unit_price=_r((s["item_price"] or 0) / max(s["quantity"] or 1, 1)),
        )
        out.append(s)
    return out


def _order_breakdown(oid, lines, order, txs) -> dict:
    """Split one order's money across its lines. Returns {line_item_id: {...}}."""
    gross = {l["line_item_id"]: (l["item_price"] or 0) + (l["delivery_charged"] or 0) for l in lines}
    order_gross = sum(gross.values())
    share = {k: (v / order_gross if order_gross else 1 / max(len(gross), 1)) for k, v in gross.items()}
    result = {k: {"deductions": defaultdict(float), "net": 0.0, "estimated": False} for k in gross}
    if not lines:
        return {}

    if not txs:
        # No Finances data yet: fall back to the fee eBay reported on the order.
        fee = (order or {}).get("marketplace_fee") or 0.0
        for k in gross:
            part = fee * share[k]
            if part:
                result[k]["deductions"][ESTIMATE_LABEL] += part
            result[k]["net"] = gross[k] - part
            result[k]["estimated"] = True
        return _finish(result)

    order_net = sum(signed(t) for t in txs)
    for t in txs:
        if t["type"] == "SALE":
            line_fees = json.loads(t["line_fees"] or "{}")
            fee_in_lines = 0.0
            for line_id, info in line_fees.items():
                target = line_id if line_id in result else None
                for f in info.get("fees", []):
                    amt = f["amount"] or 0.0
                    fee_in_lines += amt
                    _add(result, share, target, fee_label(f["feeType"]), amt)
            leftover = (t["total_fee"] or 0.0) - fee_in_lines
            if abs(leftover) > 0.004:
                _add(result, share, None, "eBay selling fees", leftover)
        else:
            # debits reduce what you made, credits (e.g. fee credits) increase it
            _add(result, share, None, tx_label(t), -signed(t))

    # Reconcile: anything eBay didn't itemise (tax handling, rounding, donations)
    explained_total = sum(sum(v["deductions"].values()) for v in result.values())
    implied = order_gross - order_net
    gap = implied - explained_total
    if abs(gap) > 0.004:
        _add(result, share, None, OTHER_LABEL, gap)

    for k in result:
        result[k]["net"] = gross[k] - sum(result[k]["deductions"].values())
    return _finish(result)


def _add(result, share, line_id, label, amount):
    if not amount:
        return
    if line_id is not None:
        result[line_id]["deductions"][label] += amount
    else:
        for k in result:
            result[k]["deductions"][label] += amount * share[k]


def _finish(result):
    for v in result.values():
        v["deductions"] = {k: _r(a) for k, a in v["deductions"].items() if abs(a) >= 0.005}
        v["net"] = _r(v["net"])
    return result


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# --------------------------------------------------------------------------- #
# Roll-ups
# --------------------------------------------------------------------------- #
def summarise(sales: list[dict]) -> dict:
    tot = {
        "count": len(sales), "units": 0, "gross": 0.0, "ebay": 0.0, "net": 0.0,
        "own": 0.0, "money_made": 0.0, "item_costs": 0.0, "bulk_alloc": 0.0, "missing_cost": 0,
        "estimated": 0, "deductions": defaultdict(float),
    }
    for s in sales:
        tot["units"] += s["quantity"] or 1
        tot["gross"] += s["gross"]
        tot["ebay"] += s["ebay_deductions_total"]
        tot["net"] += s["net_from_ebay"]
        tot["own"] += s["own_costs_total"]
        tot["money_made"] += s["money_made"]
        if s["estimated"]:
            tot["estimated"] += 1
        for k, v in s["ebay_deductions"].items():
            tot["deductions"][k] += v
        for k, v in s["own_costs"].items():
            tot["deductions"][k] += v
        if not s["in_bulk"]:
            if s["item_cost"] is None:
                tot["missing_cost"] += 1
            else:
                tot["item_costs"] += s["item_cost"]
        elif s["item_cost"] is not None:
            tot["bulk_alloc"] += s["item_cost"]
    tot["deductions"] = dict(sorted(((k, _r(v)) for k, v in tot["deductions"].items()),
                                    key=lambda kv: -kv[1]))
    for k in ("gross", "ebay", "net", "own", "money_made", "item_costs", "bulk_alloc"):
        tot[k] = _r(tot[k])
    return tot


def account_charges(conn, start: str | None, end: str | None) -> dict:
    """Charges not tied to any order (shop subscription, unlinked ad fees...)."""
    q = ("SELECT * FROM transactions WHERE (order_id IS NULL OR order_id NOT IN "
         "(SELECT order_id FROM sales WHERE order_id IS NOT NULL))")
    params = []
    if start:
        q += " AND date >= ?"
        params.append(start)
    if end:
        q += " AND date < ?"
        params.append(end)
    by = defaultdict(float)
    for t in conn.execute(q, params):
        if t["type"] in EXCLUDED_TYPES or t["type"] == "SALE":
            continue
        by[tx_label(t)] += -signed(t)
    by = {k: _r(v) for k, v in by.items() if abs(v) >= 0.005}
    return {"items": dict(sorted(by.items(), key=lambda kv: -kv[1])), "total": _r(sum(by.values()))}


def bulk_summary(bulk: dict, sales: list[dict]) -> dict:
    tot = summarise(sales)
    cost = (bulk["total_cost"] or 0) + (bulk["extra_costs"] or 0)
    profit = tot["money_made"] - cost
    return {
        **tot,
        "cost": _r(cost),
        "profit": _r(profit),
        "roi": (profit / cost * 100) if cost else None,
        "recovered_pct": (tot["money_made"] / cost * 100) if cost else None,
        "remaining_to_break_even": _r(max(cost - tot["money_made"], 0)),
        "items_left": (bulk["item_count"] - tot["units"]) if bulk.get("item_count") else None,
    }


def monthly(sales: list[dict], bulk_costs_by_month: dict) -> list[dict]:
    m = defaultdict(lambda: {"gross": 0.0, "money_made": 0.0, "costs": 0.0})
    for s in sales:
        key = (s["sold_at"] or "")[:7]
        m[key]["gross"] += s["gross"]
        m[key]["money_made"] += s["money_made"]
        if s["item_cost"] and not s["in_bulk"]:
            m[key]["costs"] += s["item_cost"]
    for key, c in bulk_costs_by_month.items():
        m[key]["costs"] += c
    rows = []
    for key in sorted(k for k in m if k):
        v = m[key]
        rows.append({"month": key, "gross": _r(v["gross"]), "money_made": _r(v["money_made"]),
                     "costs": _r(v["costs"]), "profit": _r(v["money_made"] - v["costs"])})
    return rows


def write_offs(conn, start=None, end=None) -> list[dict]:
    """Stock you wrote off (damaged, lost, donated...) in a period, valued at cost of the unsold units."""
    unit = item_unit_costs(conn)
    q = "SELECT * FROM items WHERE written_off_at IS NOT NULL"
    p = []
    if start:
        q += " AND written_off_at >= ?"
        p.append(start)
    if end:
        q += " AND written_off_at < ?"
        p.append(end)
    out = []
    for it in conn.execute(q, p):
        sold = conn.execute("SELECT COALESCE(SUM(quantity), 0) FROM sales WHERE item_id=?", (it["id"],)).fetchone()[0]
        left = max((it["quantity"] or 1) - sold, 0)
        uc = unit.get(it["id"]) or 0
        out.append({**dict(it), "units": left, "value": _r(uc * left)})
    return out


def stock_on_hand(conn) -> dict:
    """Unsold stock valued at cost: an asset, not an expense."""
    unit = item_unit_costs(conn)
    total, units, missing = 0.0, 0, 0
    for it in conn.execute("""SELECT i.id, i.quantity, (SELECT COALESCE(SUM(quantity), 0) FROM sales s
                              WHERE s.item_id = i.id AND COALESCE(s.status, '') <> 'cancelled') sold
                              FROM items i WHERE i.archived = 0 AND i.written_off_at IS NULL"""):
        left = max((it["quantity"] or 1) - it["sold"], 0)
        if not left:
            continue
        units += left
        if unit.get(it["id"]) is None:
            missing += left
        else:
            total += unit[it["id"]] * left
    return {"value": _r(total), "units": units, "missing_cost": missing}
