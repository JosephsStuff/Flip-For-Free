"""Tax figures. UK: Self Assessment (self-employment short pages, SA103S). US, Germany, Australia and Canada:
the same figures regrouped onto that country's form (see `summary` and markets.TAX).

Box numbers follow the 2026 SA103S form:
  9  turnover                         11 cost of goods bought for resale
  10 other business income            12 car, van and travel expenses
  10.1 trading income allowance       13 wages, salaries, staff costs
  14 rent, rates, power, insurance    15 repairs and maintenance
  16 accountancy, legal, professional 17 interest, bank and card charges
  18 phone, fax, stationery, office   19 other allowable business expenses
  20 total expenses                   21 net profit / 22 net loss

This is a helper for record keeping, not tax advice.
"""
from collections import defaultdict

import markets
import profit
from common import tax_year_label, tax_year_range

TRADING_ALLOWANCE = 1000.0
VAT_THRESHOLD = 90000.0

BOX_LABELS = {
    11: "Costs of goods bought for resale or goods used",
    12: "Car, van and travel expenses",
    13: "Wages, salaries and other staff costs",
    14: "Rent, rates, power and insurance costs",
    15: "Repairs and maintenance of property and equipment",
    16: "Accountancy, legal and other professional fees",
    17: "Interest and bank and credit card financial charges",
    18: "Phone, fax, stationery and other office costs",
    19: "Other allowable business expenses",
}

EXPENSE_CATEGORIES = [
    ("stock", "Stock bought (not logged in inventory)", 11),
    ("travel", "Travel: parking, trains, tolls (not mileage)", 12),
    ("wages", "Wages / paid help", 13),
    ("premises", "Storage unit, rent, power, insurance", 14),
    ("repairs", "Repairs & maintenance", 15),
    ("professional", "Accountant, legal, professional fees", 16),
    ("finance", "Bank, card & PayPal charges, interest", 17),
    ("office", "Postage, packaging, phone, internet, stationery", 18),
    ("software", "Software & subscriptions", 19),
    ("advertising", "Advertising (outside eBay)", 19),
    ("platform", "Other marketplace fees", 19),
    ("equipment", "Small equipment (scales, printer, camera)", 19),
    ("other", "Other allowable expenses", 19),
]
CATEGORY_BOX = {k: b for k, _, b in EXPENSE_CATEGORIES}
CATEGORY_LABEL = {k: l for k, l, _ in EXPENSE_CATEGORIES}

POSTAGE_LABELS = {"eBay postage labels", "Postage (paid by you)", "Packaging"}


def mileage_rates(vehicle: str, year: int):
    """(rate for first 10,000 miles, rate after) for the tax year starting in `year`."""
    if vehicle == "motorcycle":
        return 0.24, 0.24
    if vehicle == "bicycle":
        return 0.20, 0.20
    return (0.55 if year >= 2026 else 0.45), 0.25


MILEAGE_LABEL = "Business mileage"


def mileage_allowance(rows, year: int):
    """Mileage claim for one tax year: HMRC simplified rates in the UK (car/van 10,000-mile tier per tax year),
    else your market's per-mile/km rate (Australia: up to 5,000 km; Canada: none, actual costs instead)."""
    if markets.tax_code() != "UK":
        dist = sum(r["miles"] or 0 for r in rows)
        rate = markets.mileage_rate(year)
        cap = (markets.tax() or {}).get("mileage_cap")
        claimable = min(dist, cap) if cap else dist
        return {"by_vehicle": {}, "total": round(claimable * rate, 2), "miles": dist, "rate": rate,
                "capped": bool(cap and dist > cap), "cap": cap}
    totals = defaultdict(float)
    for r in rows:
        totals[r["vehicle"] or "car"] += r["miles"] or 0
    out = {"by_vehicle": {}, "total": 0.0, "miles": sum(totals.values())}
    for v, miles in totals.items():
        hi, lo = mileage_rates(v, year)
        if v in ("car", "van"):
            amt = min(miles, 10000) * hi + max(miles - 10000, 0) * lo
        else:
            amt = miles * hi
        out["by_vehicle"][v] = {"miles": miles, "amount": round(amt, 2), "rate": hi}
        out["total"] += amt
    out["total"] = round(out["total"], 2)
    return out


def compute(conn, year: int, basis: str = "cash") -> dict:
    start, end = tax_year_range(year)
    sales = profit.compute_sales(conn, "s.sold_at >= ? AND s.sold_at < ?", (start, end))
    boxes = defaultdict(float)
    lines = defaultdict(lambda: defaultdict(float))   # box -> {label: amount}

    labour_paid = (conn.execute("SELECT value FROM settings WHERE key='labour_paid'").fetchone() or [None])[0] == "1"
    gross = sum(s["gross"] for s in sales)
    refunds = sum(s["ebay_deductions"].get("Refunds", 0) for s in sales)
    turnover = gross - refunds
    by_channel = defaultdict(float)
    for s in sales:
        by_channel[s.get("channel") or "eBay"] += s["gross"] - s["ebay_deductions"].get("Refunds", 0)

    # selling costs from each sale
    for s in sales:
        for label, amt in list(s["ebay_deductions"].items()) + list(s["own_costs"].items()):
            if label == "Refunds":
                continue
            if label == profit.LABOUR:
                # your own time isn't an allowable expense; only count it if you pay someone (box 13)
                if labour_paid:
                    lines[13]["Paid packing labour"] += amt
                continue
            box = 18 if label in POSTAGE_LABELS else 19
            lines[box][label if label != "Other costs" else "Other costs on sales"] += amt
    acct = profit.account_charges(conn, start, end)
    for label, amt in acct["items"].items():
        lines[19][f"{label} (account)"] += amt

    # cost of goods
    checks = []
    if basis == "accrual":
        unallocated = 0
        for s in sales:
            if s["item_cost"] is not None:
                lines[11]["Cost of items sold" if not s["in_bulk"] else "Bulk buy share of items sold"] += s["item_cost"]
            elif s["in_bulk"]:
                unallocated += 1
        if unallocated:
            checks.append({"level": "warn", "title": f"{unallocated} bulk-buy sales have no cost share",
                           "body": "Enter the number of items in those bulk buys so the cost can be split.",
                           "link": "/bulk"})
    else:
        for it in conn.execute("""SELECT * FROM items WHERE purchased_at >= ? AND purchased_at < ?
                                  AND cost IS NOT NULL AND bulk_buy_id IS NULL""", (start, end)):
            lines[11]["Items bought"] += it["cost"] * (it["quantity"] or 1)
        for b in conn.execute("SELECT * FROM bulk_buys WHERE purchased_at >= ? AND purchased_at < ?", (start, end)):
            lines[11]["Bulk buys bought"] += (b["total_cost"] or 0) + (b["extra_costs"] or 0)
        for s in sales:
            # costs recorded only on the sale (no purchase date) are counted when sold
            if s["cost_source"] == "sale" or (s["cost_source"] == "item" and not s.get("item_purchased_at")):
                lines[11]["Cost of items sold (no purchase date)"] += s["item_cost"]

    # expense ledger
    for e in conn.execute("SELECT * FROM expenses WHERE date >= ? AND date < ?", (start, end)):
        box = CATEGORY_BOX.get(e["category"], 19)
        lines[box][CATEGORY_LABEL.get(e["category"], "Other")] += e["amount"] or 0

    # mileage
    miles = [dict(r) for r in conn.execute("SELECT * FROM mileage WHERE date >= ? AND date < ?", (start, end))]
    mil = mileage_allowance(miles, year)
    if mil["total"]:
        lines[12][MILEAGE_LABEL] += mil["total"]

    for b, items in lines.items():
        for k in list(items):
            items[k] = round(items[k], 2)
            if abs(items[k]) < 0.005:
                del items[k]
        boxes[b] = round(sum(items.values()), 2)
    total_exp = round(sum(boxes[b] for b in range(11, 20)), 2)
    box9 = round(turnover, 2)
    box10 = 0.0
    net = round(box9 + box10 - total_exp, 2)

    # trading allowance comparison
    income = box9 + box10
    allowance_profit = max(income - TRADING_ALLOWANCE, 0)
    use_allowance = TRADING_ALLOWANCE > total_exp and income > 0

    # guided data checks
    missing = [s for s in sales if s["item_cost"] is None and not s["in_bulk"] and (s.get("status") != "cancelled")]
    if missing:
        checks.append({"level": "warn", "title": f"{len(missing)} sales have no cost",
                       "body": "Add what you paid so your profit and cost of goods are right.",
                       "link": f"/sales?filter=missing_cost&period=custom&start={start}&end={_minus_day(end)}"})
    est = [s for s in sales if s["estimated"]]
    if est:
        checks.append({"level": "warn", "title": f"{len(est)} sales use estimated eBay fees",
                       "body": "Run a sync so the exact fees come in from eBay.", "link": "/settings"})
    if basis == "cash":
        nodate = conn.execute("""SELECT COUNT(*) FROM items WHERE cost IS NOT NULL AND purchased_at IS NULL
                                 AND bulk_buy_id IS NULL AND archived=0""").fetchone()[0]
        if nodate:
            checks.append({"level": "info", "title": f"{nodate} inventory items have a cost but no purchase date",
                           "body": "On the cash basis, stock counts in the tax year you bought it.",
                           "link": "/inventory?filter=no_date"})
        nodate_b = conn.execute("SELECT COUNT(*) FROM bulk_buys WHERE purchased_at IS NULL").fetchone()[0]
        if nodate_b:
            checks.append({"level": "info", "title": f"{nodate_b} bulk buys have no purchase date",
                           "body": "Add the date you bought them.", "link": "/bulk"})
    if any(not m.get("purpose") for m in miles):
        checks.append({"level": "info", "title": "Some mileage trips have no purpose",
                       "body": "Tax authorities expect a business reason for each journey.", "link": f"/mileage?year={year}"})
    if total_exp and any(e for e in lines[12] if "mileage" not in e.lower()) and mil["total"]:
        checks.append({"level": "info", "title": "You have both mileage and travel costs",
                       "body": "You can't claim fuel or running costs for a vehicle you also claim mileage for.",
                       "link": f"/expenses?year={year}"})

    return {
        "year": year, "label": tax_year_label(year), "start": start, "end": end, "basis": basis,
        "box9": box9, "box10": box10, "boxes": {b: boxes.get(b, 0.0) for b in range(11, 20)},
        "lines": {b: dict(sorted(lines[b].items(), key=lambda kv: -kv[1])) for b in range(11, 20)},
        "box20": total_exp, "box21": max(net, 0), "box22": max(-net, 0), "net": net,
        "sales_count": len(sales), "gross": round(gross, 2), "refunds": round(refunds, 2),
        "by_channel": {k: round(v, 2) for k, v in sorted(by_channel.items(), key=lambda kv: -kv[1])},
        "mileage": mil, "trading_allowance": {
            "allowance": TRADING_ALLOWANCE, "under": income <= TRADING_ALLOWANCE,
            "profit_with_allowance": round(allowance_profit, 2), "better": use_allowance,
        },
        "over_vat": income > VAT_THRESHOLD, "checks": checks, "sales": sales,
    }


def summary(conn, year: int, basis: str = "cash") -> dict:
    """The year's figures on your market's form lines (US Schedule C, German EÜR, Australian business schedule,
    Canadian T2125). Record-keeping aid, not tax advice."""
    r = compute(conn, year, basis)
    t = markets.tax()
    rows = []
    mil = r["mileage"]["total"]
    if mil and t.get("mileage_line"):
        rows.append({"line": t["mileage_line"], "amount": mil, "items": {}})
    for b in range(11, 20):
        items = {k: v for k, v in r["lines"][b].items() if k != MILEAGE_LABEL}
        amt = round(sum(items.values()), 2)
        if items:
            rows.append({"line": t["lines"][b], "amount": amt, "items": items})
    merged = {}
    for row in rows:
        m = merged.setdefault(row["line"], {"line": row["line"], "amount": 0.0, "items": {}})
        m["amount"] = round(m["amount"] + row["amount"], 2)
        m["items"].update(row["items"])
    rows = list(merged.values())
    total = round(sum(x["amount"] for x in rows), 2)
    income = r["box9"] + r["box10"]
    limit, limit_label = t.get("threshold") or (None, None)
    return {**r, "profile": t, "rows": rows, "last_day": _minus_day(r["end"]), "total_expenses": total, "income": round(income, 2),
            "profit": round(income - total, 2), "threshold": limit, "threshold_label": limit_label,
            "threshold_pct": min(income / limit * 100, 100) if limit else None}


def _minus_day(iso):
    from datetime import date, timedelta
    return (date.fromisoformat(iso) - timedelta(days=1)).isoformat()
