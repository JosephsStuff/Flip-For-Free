"""Generates realistic fake eBay data (shaped exactly like the real API responses)
so you can try the app without connecting an account:  python app.py --demo
"""
import random
import zlib
from datetime import datetime, timedelta, timezone

import db
from db import connect, init_db, Settings, write_lock, now_iso
from sync import _save_order, _save_transaction, detect_issues, link_sales_to_items, sync_listings
from markets import money as fmt_money

UK_POSTCODES = ["SW1A 1AA", "M1 1AE", "B1 1BB", "LS1 4AP", "G1 1XQ", "EH1 1YZ", "CF10 1EP", "BS1 4DJ", "NE1 7RU",
                "L1 8JQ", "NG1 5FS", "S1 2HE", "BT1 5GS", "AB10 1XG", "PL1 2AA", "NR1 3QU", "CB2 1TN", "OX1 2JD",
                "BN1 1AE", "IP1 1AA", "CO1 1AA", "EX1 1AA", "YO1 7HH", "LE1 5WW", "SO14 2AQ", "DE1 2FS", "E1 6AN"]
INTL = [("IE", "D02 X285", "Dublin"), ("US", "10001", "New York"), ("DE", "10115", "Berlin"),
        ("FR", "75001", "Paris"), ("AU", "2000", "Sydney"), ("NL", "1012 AB", "Amsterdam")]
SOURCES = {"LOB": "Car boot", "SDY": "Car boot", "Monster": "Car boot", "LEGO": "House clearance",
           "Casio": "House clearance", "Nintendo": "Facebook Marketplace", "PS2": "Facebook Marketplace",
           "Walkman": "Facebook Marketplace", "Pokemon": "eBay auction"}
LOCATIONS = ["Shelf A1", "Shelf A2", "Shelf B1", "Card binder 1", "Card binder 2", "Box 3", "Garage rack"]

ITEMS = [
    ("Blue-Eyes White Dragon LOB-001 1st Edition", 85, 160),
    ("Dark Magician SDY-006 Ultra Rare", 18, 40),
    ("Red-Eyes Black Dragon LOB-070", 25, 60),
    ("Exodia the Forbidden One LOB-124", 30, 75),
    ("Pot of Greed LOB-119 Rare", 6, 15),
    ("Monster Reverse Holo Bundle x20", 8, 18),
    ("LEGO Star Wars 75192 minifigure lot", 22, 55),
    ("Nintendo DS Lite - Cobalt Blue", 35, 65),
    ("Pokemon Base Set Charizard 4/102", 180, 320),
    ("Vintage Casio F-91W Watch", 9, 20),
    ("PS2 Game Bundle x5", 12, 30),
    ("Sony Walkman WM-EX194", 25, 50),
]
POSTAGE = [0, 0, 1.55, 2.70, 3.49, 4.20]


def _amt(v):
    return {"value": f"{v:.2f}", "currency": "GBP"}


def _addr(rnd):
    if rnd.random() < 0.88:
        pc = rnd.choice(UK_POSTCODES)
        return {"postalCode": pc, "countryCode": "GB", "city": ""}
    c, pc, city = rnd.choice(INTL)
    return {"postalCode": pc, "countryCode": c, "city": city}


def seed(path: str, set_path: bool = True):
    """Fill `path` with a year of fake data. set_path=False leaves the app's database setting alone (showcase)."""
    init_db(path)
    if set_path:
        db.set_db_path(path)
    rnd = random.Random(42)
    settings = Settings(path)
    settings.set_many({
        "client_id": "DEMO", "client_secret": "DEMO", "ru_name": "DEMO",
        "owner_user_id": "demo", "owner_username": "demo_seller",
        "last_sync_orders": now_iso(), "last_sync_finances": now_iso(), "demo": "1",
    })
    conn = connect(path)
    now = datetime.now(timezone.utc)

    with write_lock():
        bulk = []
        for name, days_ago, cost, extra, count, src, loc in [
            ("Car boot Yu-Gi-Oh binder", 200, 180.0, 0, 120, "Car boot", "Card binder 1"),
            ("Facebook retro gaming lot", 120, 95.0, 12.0, 30, "Facebook Marketplace", "Garage rack"),
            ("Job lot - house clearance", 45, 60.0, 0, 25, "House clearance", "Box 3"),
        ]:
            cur = conn.execute(
                "INSERT INTO bulk_buys(name, purchased_at, total_cost, extra_costs, item_count, "
                "notes, created_at, source, location) VALUES (?,?,?,?,?,?,?,?,?)",
                (name, (now - timedelta(days=days_ago)).date().isoformat(), cost, extra, count,
                 "Demo bulk buy", now_iso(), src, loc),
            )
            bulk.append((cur.lastrowid, now - timedelta(days=days_ago)))
        conn.commit()

    tx_n = 0
    for i in range(180):
        days_ago = rnd.randint(0, 240) if i < 140 else rnd.randint(241, 420)
        created = now - timedelta(days=days_ago, hours=rnd.randint(0, 23))
        oid = f"{rnd.randint(10,27)}-{rnd.randint(10000,99999)}-{rnd.randint(10000,99999)}"
        n_lines = 1 if rnd.random() < 0.88 else 2
        lines, subtotal, delivery = [], 0.0, 0.0
        for j in range(n_lines):
            title, lo, hi = rnd.choice(ITEMS)
            legacy = str(zlib.crc32(title.encode())).zfill(12)
            qty = 1 if rnd.random() < 0.9 else 2
            price = round(rnd.uniform(lo, hi) * qty, 2)
            ship = rnd.choice(POSTAGE) if j == 0 else 0.0
            subtotal += price
            delivery += ship
            lines.append({
                "lineItemId": f"{rnd.randint(10**10, 10**11)}", "legacyItemId": legacy,
                "sku": "", "title": title, "quantity": qty,
                "lineItemCost": _amt(price),
                "deliveryCost": {"shippingCost": _amt(ship)},
            })
        total = subtotal + delivery
        fvf_total = 0.0
        tx_lines = []
        for li in lines:
            basis = float(li["lineItemCost"]["value"]) + float(li["deliveryCost"]["shippingCost"]["value"])
            fvf = round(basis * 0.128, 2)
            rof = round(basis * 0.0035, 2)
            fees = [{"feeType": "FINAL_VALUE_FEE", "amount": _amt(fvf)},
                    {"feeType": "REGULATORY_OPERATING_FEE", "amount": _amt(rof)}]
            fvf_total += fvf + rof
            tx_lines.append({"lineItemId": li["lineItemId"], "feeBasisAmount": _amt(basis),
                             "marketplaceFees": fees})
        per_order = 0.30 if total > 10 else 0.20
        tx_lines[0]["marketplaceFees"].append(
            {"feeType": "FINAL_VALUE_FEE_FIXED_PER_ORDER", "amount": _amt(per_order)})
        fees_total = round(fvf_total + per_order, 2)
        refunded = rnd.random() < 0.03
        order = {
            "orderId": oid, "creationDate": created.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "buyer": {"username": f"buyer{rnd.randint(100,999)}"},
            "orderPaymentStatus": "FULLY_REFUNDED" if refunded else "PAID",
            "cancelStatus": {"cancelState": "NONE_REQUESTED"},
            "pricingSummary": {"priceSubtotal": _amt(subtotal), "deliveryCost": _amt(delivery),
                               "total": _amt(total)},
            "totalMarketplaceFee": _amt(fees_total),
            "lineItems": lines,
            "fulfillmentStartInstructions": [{"shippingStep": {"shipTo": {"contactAddress": _addr(rnd)}}}],
        }
        _save_order(conn, order)

        txs = [{
            "transactionId": f"T{tx_n}", "transactionType": "SALE", "orderId": oid,
            "bookingEntry": "CREDIT", "amount": _amt(total - fees_total),
            "totalFeeAmount": _amt(fees_total), "totalFeeBasisAmount": _amt(total),
            "transactionDate": (created + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "orderLineItems": tx_lines,
        }]
        tx_n += 1
        if rnd.random() < 0.35:
            txs.append({"transactionId": f"T{tx_n}", "transactionType": "NON_SALE_CHARGE",
                        "feeType": "AD_FEE", "bookingEntry": "DEBIT",
                        "amount": _amt(round(total * rnd.choice([0.02, 0.04, 0.06]), 2)),
                        "references": [{"referenceId": oid, "referenceType": "ORDER_ID"}],
                        "transactionDate": (created + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")})
            tx_n += 1
        if delivery and rnd.random() < 0.5:
            txs.append({"transactionId": f"T{tx_n}", "transactionType": "SHIPPING_LABEL",
                        "orderId": oid, "bookingEntry": "DEBIT",
                        "amount": _amt(round(delivery * rnd.uniform(0.8, 1.1), 2)),
                        "transactionDate": (created + timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M:%S.000Z")})
            tx_n += 1
        if refunded:
            txs.append({"transactionId": f"T{tx_n}", "transactionType": "REFUND", "orderId": oid,
                        "bookingEntry": "DEBIT", "amount": _amt(total),
                        "transactionDate": (created + timedelta(days=6)).strftime("%Y-%m-%dT%H:%M:%S.000Z")})
            tx_n += 1
            txs.append({"transactionId": f"T{tx_n}", "transactionType": "CREDIT", "orderId": oid,
                        "bookingEntry": "CREDIT", "amount": _amt(round(fvf_total, 2)),
                        "transactionMemo": "Final value fee credit",
                        "transactionDate": (created + timedelta(days=6)).strftime("%Y-%m-%dT%H:%M:%S.000Z")})
            tx_n += 1
        for t in txs:
            _save_transaction(conn, t)
    # monthly shop subscription (account-level, not tied to an order)
    for m in range(8):
        d = now - timedelta(days=30 * m + 3)
        _save_transaction(conn, {"transactionId": f"SUB{m}", "transactionType": "NON_SALE_CHARGE",
                                 "feeType": "SUBSCRIPTION_FEE", "bookingEntry": "DEBIT",
                                 "amount": _amt(7.19),
                                 "transactionDate": d.strftime("%Y-%m-%dT%H:%M:%S.000Z")})
    conn.commit()

    # assign costs: Yu-Gi-Oh to bulk 1, games to bulk 2, some random to bulk 3, rest own cost
    with write_lock():
        rows = conn.execute("SELECT line_item_id, title, item_price, sold_at FROM sales").fetchall()
        for r in rows:
            t = r["title"]
            sold = r["sold_at"]
            if any(k in t for k in ("LOB", "SDY", "Monster Reverse")) and sold >= bulk[0][1].isoformat()[:10]:
                conn.execute("UPDATE sales SET bulk_buy_id=? WHERE line_item_id=?", (bulk[0][0], r["line_item_id"]))
            elif any(k in t for k in ("Nintendo", "PS2", "Walkman")) and sold >= bulk[1][1].isoformat()[:10]:
                conn.execute("UPDATE sales SET bulk_buy_id=? WHERE line_item_id=?", (bulk[1][0], r["line_item_id"]))
            elif any(k in t for k in ("Casio", "LEGO")) and sold >= bulk[2][1].isoformat()[:10]:
                conn.execute("UPDATE sales SET bulk_buy_id=?, packaging_cost=0.35 WHERE line_item_id=?",
                             (bulk[2][0], r["line_item_id"]))
            elif rnd.random() < 0.85:
                conn.execute("UPDATE sales SET cost=?, packaging_cost=? WHERE line_item_id=?",
                             (round(r["item_price"] * rnd.uniform(0.25, 0.6), 2),
                              rnd.choice([None, 0.25, 0.50]), r["line_item_id"]))
        conn.commit()
    _seed_v2(conn, rnd, now, bulk)
    conn.close()


def _iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _seed_v2(conn, rnd, now, bulk):
    """Inventory, listings, other marketplaces, expenses, mileage, automations."""
    import automation
    # multi-quantity listings for the titles that have been selling
    listings = []
    for title, lo, hi in ITEMS:
        legacy = str(zlib.crc32(title.encode())).zfill(12)
        sold = conn.execute("SELECT COALESCE(SUM(quantity),0) FROM sales WHERE legacy_item_id=?", (legacy,)).fetchone()[0]
        avail = rnd.choice([0, 1, 2, 3])
        if not avail:
            continue
        listings.append({"legacy_item_id": legacy, "title": title, "sku": None, "price": round((lo + hi) / 2, 2),
                         "currency": "GBP", "quantity": sold + avail, "quantity_sold": sold, "quantity_available": avail,
                         "listing_type": "FixedPriceItem", "duration": "GTC",
                         "start_time": _iso(now - timedelta(days=rnd.randint(60, 300))), "end_time": None,
                         "watch_count": rnd.randint(0, 9), "image_url": None, "url": None, "bids": 0})
    # single items for automations
    singles = [("Yu-Gi-Oh Mirror Force MRD-138", 14), ("Pokemon Jungle Booster Pack (empty) ", 9),
               ("Sony PSP 3000 Piano Black", 65), ("Tamagotchi Connection V2", 28), ("Polaroid 600 Camera", 35),
               ("Harry Potter Philosopher's Stone PB 1st", 40), ("Game Boy Color – Atomic Purple", 55),
               ("Seiko 5 Automatic 7S26", 70), ("Pokemon Base Set Blastoise 2/102", 120), ("Lot 10 N64 games", 85),
               ("Yu-Gi-Oh Jinzo PSV-000", 22), ("Vintage Levi's 501 W32 L32", 30)]
    for k, (title, price) in enumerate(singles):
        age = [29.3, 29.6, 28.9, 5, 12, 22, 35, 59.4, 41, 8, 26, 16][k]
        listings.append({"legacy_item_id": str(395000000000 + k * 7919), "title": title.strip(), "sku": f"DEMO-{k}",
                         "price": float(price), "currency": "GBP", "quantity": 1, "quantity_sold": 0,
                         "quantity_available": 1, "listing_type": "FixedPriceItem", "duration": "GTC",
                         "start_time": _iso(now - timedelta(days=age)), "end_time": None,
                         "watch_count": [0, 1, 4, 0, 2, 0, 1, 0, 6, 0, 0, 3][k], "image_url": None, "url": None, "bids": 0})
    sync_listings(conn, listings)
    with write_lock():
        unit_bulk = {"LOB": bulk[0][0], "SDY": bulk[0][0], "Monster": bulk[0][0], "Mirror Force": bulk[0][0],
                     "Jinzo": bulk[0][0], "Nintendo": bulk[1][0], "PS2": bulk[1][0], "Walkman": bulk[1][0],
                     "N64": bulk[1][0], "LEGO": bulk[2][0], "Casio": bulk[2][0]}
        for it in conn.execute("SELECT id, title FROM items").fetchall():
            src = next((v for k, v in SOURCES.items() if k in it["title"]), rnd.choice(
                ["Charity shop", "Car boot", "Vinted", "Facebook Marketplace"]))
            b = next((v for k, v in unit_bulk.items() if k in it["title"]), None)
            cost = None if b else round(rnd.uniform(3, 30), 2)
            bought = (now - timedelta(days=rnd.randint(70, 330))).date().isoformat()
            conn.execute("UPDATE items SET source=?, location=?, bulk_buy_id=?, cost=?, purchased_at=? WHERE id=?",
                         (src, rnd.choice(LOCATIONS), b, cost, bought, it["id"]))
        # the valuable card in the binder gets a fixed cost
        conn.execute("""UPDATE items SET cost=8, cost_locked=1 WHERE title LIKE '%Mirror Force%'""")
        # promote a few
        for lid in [l["legacy_item_id"] for l in listings[-4:-2]]:
            conn.execute("UPDATE listings SET promoted=1 WHERE legacy_item_id=?", (lid,))
        conn.execute("UPDATE bulk_buys SET split_method='price' WHERE id=?", (bulk[0][0],))
        # unlisted stock
        for title, cost, src, days in [("Nintendo Wii Fit Board", 4.0, "Charity shop", 6), ("Box of 30 Pokemon commons", 2.5, "Car boot", 9),
                                       ("Sony Discman D-E350", 6.0, "Car boot", 13), ("Lego Technic 8860 (incomplete)", 18.0, "Facebook Marketplace", 20),
                                       ("Vintage Pyrex bowl set", 5.0, "House clearance", 30), ("Yu-Gi-Oh Starter Deck Kaiba (sealed)", 15.0, "Vinted", 3)]:
            cur = conn.execute("""INSERT INTO items(title, quantity, cost, source, purchased_at, location, created_at, updated_at)
                                  VALUES (?,1,?,?,?,?,?,?)""", (title, cost, src, (now - timedelta(days=days)).date().isoformat(),
                                                                rnd.choice(LOCATIONS), now_iso(), now_iso()))
            db.add_event(conn, cur.lastrowid, "created", f"Bought from {src} for {fmt_money(cost)}",
                         at=(now - timedelta(days=days)).date().isoformat())
        conn.commit()
    link_sales_to_items(conn)
    with write_lock():
        # sales of listing-linked items now carry item costs; clear duplicated sale-level costs
        conn.execute("UPDATE sales SET cost=NULL WHERE item_id IS NOT NULL")
        # other marketplaces
        for k in range(14):
            d = now - timedelta(days=rnd.randint(1, 300))
            ch = rnd.choice(["Vinted", "Vinted", "Facebook Marketplace", "Depop", "Car boot"])
            title = rnd.choice(["Nike Air Max 90 UK9", "Zara wool coat M", "Pokemon plush bundle", "Retro Adidas track top",
                                "Barbour wax jacket L", "Kindle Paperwhite 10th gen", "Dr Martens 1460 UK7"])
            price = round(rnd.uniform(12, 70), 2)
            fees = 0 if ch in ("Vinted", "Facebook Marketplace", "Car boot") else round(price * 0.10, 2)
            lid = f"M-demo{k}"
            conn.execute("""INSERT INTO sales(line_item_id, order_id, title, quantity, item_price, delivery_charged, manual_fees,
                            postage_cost, cost, channel, sold_at, source, currency, updated_at)
                            VALUES (?,?,?,1,?,0,?,?,?,?,?, 'manual', 'GBP', ?)""",
                         (lid, lid, title, price, fees, 3.2 if ch != "Car boot" else None, round(price * rnd.uniform(.2, .5), 2),
                          ch, _iso(d), now_iso()))
        # single-quantity eBay listings that sold, for time to sell
        for k in range(22):
            listed = now - timedelta(days=rnd.randint(20, 320))
            days = rnd.choice([rnd.uniform(0.5, 7), rnd.uniform(7, 28), rnd.uniform(7, 28), rnd.uniform(28, 91),
                               rnd.uniform(91, 160)])
            sold_at = min(listed + timedelta(days=days), now - timedelta(hours=6))
            title = rnd.choice(["Pokemon Base Set Charizard 4/102", "Sony Walkman WM-EX10", "Casio F-91W boxed",
                                "Nintendo Game Boy Advance SP", "Vintage Levi's 505 W34", "Lego 75192 manual set",
                                "Seiko SNK809", "Yu-Gi-Oh Dark Magician SDY-006", "Kodak Brownie camera",
                                "Pokemon Neo Genesis Lugia", "Zelda Ocarina of Time N64"]) + f" #{k + 1}"
            price = round(rnd.uniform(12, 110), 2)
            lid = str(396000000000 + k * 104729)
            conn.execute("""INSERT OR IGNORE INTO listings(legacy_item_id, title, price, currency, quantity, quantity_available,
                            quantity_sold, listing_type, start_time, end_time, status, updated_at)
                            VALUES (?,?,?, 'GBP', 1, 0, 1, 'FixedPriceItem', ?, ?, 'sold', ?)""",
                         (lid, title, price, _iso(listed), _iso(sold_at), now_iso()))
            conn.execute("""INSERT OR IGNORE INTO sales(line_item_id, order_id, legacy_item_id, title, quantity, item_price,
                            delivery_charged, manual_fees, postage_cost, cost, sold_at, source, currency, updated_at)
                            VALUES (?,?,?,?,1,?,3.2,?,3.2,?,?, 'ebay', 'GBP', ?)""",
                         (f"S-demo{k}", f"O-demo{k}", lid, title, price, round(price * 0.128 + 0.3, 2),
                          round(price * rnd.uniform(.2, .5), 2), _iso(sold_at), now_iso()))
        # expenses & mileage
        for k in range(16):
            d = (now - timedelta(days=rnd.randint(1, 400))).date().isoformat()
            cat, desc, amt = rnd.choice([("office", "Bubble mailers x200", 18.99), ("office", "Thermal labels", 12.50),
                                         ("premises", "Storage unit (monthly)", 45.0), ("software", "Photo editing app", 4.99),
                                         ("equipment", "Postal scales", 21.0), ("finance", "PayPal fees", 3.40),
                                         ("office", "Card sleeves & toploaders", 9.80)])
            conn.execute("INSERT INTO expenses(date, category, description, amount, created_at) VALUES (?,?,?,?,?)",
                         (d, cat, desc, amt, now_iso()))
        for k in range(22):
            d = (now - timedelta(days=rnd.randint(1, 400))).date().isoformat()
            conn.execute("INSERT INTO mileage(date, miles, vehicle, purpose, route, created_at) VALUES (?,?,?,?,?,?)",
                         (d, round(rnd.uniform(4, 38), 1) * 2, "car", rnd.choice(
                             ["Sourcing at car boot", "Charity shop run", "Collect Facebook lot"]),
                          "Home → " + rnd.choice(["Ipswich car boot", "Colchester", "Town centre", "Sudbury"]), now_iso()))
        # dispatch dates (0-2 days after sale) and a drop-off point
        for r in conn.execute("SELECT line_item_id, sold_at, order_id FROM sales WHERE source='ebay'").fetchall():
            d = datetime.fromisoformat(r["sold_at"].replace("Z", "+00:00")) + timedelta(days=rnd.choice([0, 1, 1, 2]))
            conn.execute("UPDATE sales SET shipped_at=? WHERE order_id=?", (_iso(d), r["order_id"]))
        conn.execute("""INSERT INTO drop_off_locations(name, postcode, miles_one_way, distance_note, is_default, created_at)
                        VALUES ('Post Office – Carr Street', 'IP4 1AA', 2.3, 'driving distance (OpenStreetMap)', 1, ?),
                               ('InPost locker – Tesco', 'IP3 9QR', 3.8, 'driving distance (OpenStreetMap)', 0, ?)""",
                     (now_iso(), now_iso()))
        conn.executemany("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                         [("home_postcode", "IP1 3AB"), ("mileage_vehicle", "car")])
        # a duplicate postage label to show error detection
        r = conn.execute("SELECT order_id FROM transactions WHERE type='SHIPPING_LABEL' ORDER BY date DESC LIMIT 1").fetchone()
        if r:
            conn.execute("""INSERT INTO transactions(transaction_id, type, order_id, booking, amount, currency, date, line_fees, refs, raw)
                            VALUES ('DUPLABEL', 'SHIPPING_LABEL', ?, 'DEBIT', 3.35, 'GBP', ?, '{}', '[]', '{}')""",
                         (r["order_id"], _iso(now - timedelta(days=2))))
        # a recent sale notification
        r = conn.execute("""SELECT s.line_item_id, s.title, s.item_price, i.location FROM sales s LEFT JOIN items i ON i.id=s.item_id
                            WHERE s.source='ebay' ORDER BY s.sold_at DESC LIMIT 2""").fetchall()
        for x in r:
            db.notify(conn, "sale", f"Sold: {x['title']}", f"{fmt_money(x['item_price'])} · Stored at: {x['location'] or '—'}",
                      f"/sales/{x['line_item_id']}", key=f"sale-{x['line_item_id']}")
        conn.commit()
    detect_issues(conn)
    with write_lock():
        r = conn.execute("SELECT id FROM items WHERE title LIKE 'Vintage Pyrex%'").fetchone()
        if r:
            conn.execute("UPDATE items SET written_off_at=?, write_off_reason='Damaged' WHERE id=?",
                         ((now - timedelta(days=4)).date().isoformat(), r["id"]))
        for r in conn.execute("""SELECT id FROM items WHERE purchased_at IS NOT NULL ORDER BY id LIMIT 5""").fetchall():
            conn.execute("UPDATE items SET purchased_at=? WHERE id=?", ((now - timedelta(days=150)).date().isoformat(), r["id"]))
    conn.commit()
    automation.ensure_rules(conn)
    with write_lock():
        conn.execute("UPDATE automation_rules SET enabled=1")
        conn.commit()
    automation.run_all(simulate=True, triggered_by="manual")
