"""Pull orders and money movements from eBay into the database."""
import json
import re
import threading
import traceback
from datetime import datetime, timedelta, timezone

from db import Settings, add_event, connect, device_name, notify, now_iso, write_lock
from ebay_api import EbayClient, EbayError, currency, dumps, money

STATUS = {"running": False, "message": "", "last_result": None, "progress": ""}
_lock = threading.Lock()

PAID_STATES = {"PAID", "PARTIALLY_REFUNDED", "FULLY_REFUNDED"}
UK_OUTCODE = re.compile(r"^([A-Z]{1,2}\d[A-Z\d]?)\s*\d[A-Z]{2}$")


def outcode(postcode: str | None, country: str | None) -> str | None:
    """Only the postcode district (e.g. 'SW1A') is kept - never the full address."""
    if not postcode:
        return None
    pc = postcode.upper().strip()
    if country == "GB":
        m = UK_OUTCODE.match(pc)
        return m.group(1) if m else pc.split()[0][:4]
    return None


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def start_background(full: bool = False) -> bool:
    if not _lock.acquire(blocking=False):
        return False
    t = threading.Thread(target=_run_locked, args=(full,), daemon=True)
    t.start()
    return True


def _run_locked(full):
    try:
        run_sync(full)
    finally:
        _lock.release()


def run_sync(full: bool = False) -> dict:
    settings = Settings()
    STATUS.update(running=True, message="Starting sync...", progress="")
    conn = connect()
    log_id = None
    with write_lock():
        cur = conn.execute(
            "INSERT INTO sync_log(started_at, status, device) VALUES (?, 'running', ?)",
            (now_iso(), device_name()),
        )
        log_id = cur.lastrowid
        conn.commit()
    result = {"orders": 0, "sales": 0, "transactions": 0, "finances_error": None, "listings": 0}
    try:
        client = EbayClient(settings)
        s = settings.get_many(["last_sync_orders", "last_sync_finances", "first_sync_days"])
        first_days = int(s.get("first_sync_days") or 730)
        now = datetime.now(timezone.utc)
        default_start = now - timedelta(days=first_days)

        # ---------------- Orders (Fulfillment API) ----------------
        last_o = None if full else _parse(s.get("last_sync_orders"))
        if last_o:
            since, by_modified = last_o - timedelta(days=3), True
        else:
            since, by_modified = default_start, False
        STATUS["message"] = "Downloading orders..."
        n = 0
        for order in client.iter_orders(since, by_modified):
            result["sales"] += _save_order(conn, order)
            # Save each one straight away: the next item may mean waiting on eBay for a new page, and an open
            # write would lock everyone else (other pages, automations) out of the database meanwhile.
            conn.commit()
            n += 1
            if n % 50 == 0:
                STATUS["progress"] = f"{n} orders"
        result["orders"] = n
        settings.set("last_sync_orders", now.strftime("%Y-%m-%dT%H:%M:%SZ"))

        # ---------------- Money (Finances API) ----------------
        last_f = None if full else _parse(s.get("last_sync_finances"))
        # re-read 14 days each time: late ad fees / refunds / label voids
        f_since = (last_f - timedelta(days=14)) if last_f else default_start
        STATUS.update(message="Downloading fees, refunds and charges...", progress="")
        try:
            m = 0
            for tx in client.iter_transactions(f_since, now):
                _save_transaction(conn, tx)
                conn.commit()                      # see above: never hold a write while waiting on eBay
                m += 1
                if m % 200 == 0:
                    STATUS["progress"] = f"{m} transactions"
            result["transactions"] = m
            settings.set("last_sync_finances", now.strftime("%Y-%m-%dT%H:%M:%SZ"))
        except EbayError as e:
            # Orders still useful on their own (fees fall back to estimates).
            result["finances_error"] = str(e)

        # ---------------- Dispatch dates (for post office mileage) ----------------
        STATUS.update(message="Checking dispatch dates...", progress="")
        try:
            last_d = _parse(settings.get("last_dispatch_check"))
            if not last_d or datetime.now(timezone.utc) - last_d > timedelta(minutes=55):
                fill_dispatch_dates(conn, client)
                settings.set("last_dispatch_check", now.strftime("%Y-%m-%dT%H:%M:%SZ"))
        except EbayError as e:
            result["dispatch_error"] = str(e)
        import dispatch
        dispatch.auto_log(conn)

        # ---------------- Active listings (Trading API) ----------------
        STATUS.update(message="Downloading your active listings...", progress="")
        try:
            result["listings"] = sync_listings(conn, client.iter_active_listings())
        except EbayError as e:
            result["listings_error"] = str(e)
        link_sales_to_items(conn)
        detect_issues(conn)
        conn.commit()
        STATUS.update(message="Checking your other shops...", progress="")
        try:
            import stores
            result["stores"] = stores.sync_stores(conn)
        except Exception as e:  # noqa: BLE001 - other shops must never break the eBay sync
            import errors
            errors.record("Other shops sync", e)
            result["stores"] = f"Other shops failed: {e}"
        try:
            import features2
            features2.monthly_recap(conn)
        except Exception as e:  # noqa: BLE001
            print("monthly recap failed:", e)

        msg = (f"{result['orders']} orders, {result['transactions']} money transactions, "
               f"{result.get('listings', 0)} active listings synced.")
        if result.get("stores"):
            msg += " Other shops: " + result["stores"][:200] + "."
        if result.get("listings_error"):
            msg += " Listings failed: " + result["listings_error"][:200]
        if result["finances_error"]:
            msg += " Finances API failed - fees shown are estimates. " + result["finances_error"][:300]
        status = "partial" if result["finances_error"] else "ok"
    except Exception as e:  # noqa: BLE001
        import errors
        errors.record("eBay sync", e)
        msg, status = f"Sync failed: {e}", "error"
    finally:
        with write_lock():
            conn.execute(
                "UPDATE sync_log SET finished_at=?, status=?, message=? WHERE id=?",
                (now_iso(), status, msg, log_id),
            )
            conn.commit()
        conn.close()
        STATUS.update(running=False, message=msg, progress="", last_result=status)
    return result


def _save_order(conn, o: dict) -> int:
    pay = o.get("orderPaymentStatus")
    cancel = (o.get("cancelStatus") or {}).get("cancelState")
    ship = {}
    for fsi in o.get("fulfillmentStartInstructions") or []:
        addr = ((fsi.get("shippingStep") or {}).get("shipTo") or {}).get("contactAddress") or {}
        if addr:
            ship = addr
            break
    country = ship.get("countryCode")
    district = outcode(ship.get("postalCode"), country)
    city = ship.get("city")
    if cancel == "CANCELED":
        status = "cancelled"
    elif pay == "FULLY_REFUNDED":
        status = "refunded"
    elif pay == "PARTIALLY_REFUNDED":
        status = "part refunded"
    else:
        status = None
    ps = o.get("pricingSummary") or {}
    cur = currency(ps.get("total"))
    with write_lock():
        conn.execute(
            """INSERT INTO orders(order_id, created_at, buyer, payment_status, cancel_state, currency,
                   subtotal, delivery, total, marketplace_fee, raw, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(order_id) DO UPDATE SET
                   payment_status=excluded.payment_status, cancel_state=excluded.cancel_state,
                   subtotal=excluded.subtotal, delivery=excluded.delivery, total=excluded.total,
                   marketplace_fee=excluded.marketplace_fee, raw=excluded.raw,
                   updated_at=excluded.updated_at, buyer=excluded.buyer""",
            (
                o["orderId"], o.get("creationDate"), (o.get("buyer") or {}).get("username"),
                pay, cancel, cur, money(ps.get("priceSubtotal")), money(ps.get("deliveryCost")),
                money(ps.get("total")), money(o.get("totalMarketplaceFee")), dumps(o), now_iso(),
            ),
        )
        if pay not in PAID_STATES:
            return 0
        count = 0
        for li in o.get("lineItems", []):
            line_id = li["lineItemId"]
            legacy = li.get("legacyItemId")
            existed = conn.execute("SELECT 1 FROM sales WHERE line_item_id=?", (line_id,)).fetchone()
            qty = int(li.get("quantity") or 1)
            conn.execute(
                """INSERT INTO sales(line_item_id, order_id, legacy_item_id, sku, title, quantity,
                       item_price, delivery_charged, currency, sold_at, source, updated_at, channel,
                    status, ship_country, ship_outcode, ship_city)
                   VALUES (?,?,?,?,?,?,?,?,?,?, 'ebay', ?, 'eBay', ?,?,?,?)
                   ON CONFLICT(line_item_id) DO UPDATE SET
                       title=excluded.title, quantity=excluded.quantity,
                       item_price=excluded.item_price, delivery_charged=excluded.delivery_charged,
                       sku=excluded.sku, updated_at=excluded.updated_at, status=excluded.status,
                       ship_country=excluded.ship_country, ship_outcode=excluded.ship_outcode,
                       ship_city=excluded.ship_city""",
                (
                    line_id, o["orderId"], legacy, li.get("sku"), li.get("title"), qty,
                    money(li.get("lineItemCost")),
                    money((li.get("deliveryCost") or {}).get("shippingCost")),
                    currency(li.get("lineItemCost"), cur), o.get("creationDate"), now_iso(),
                    status, country, district, city,
                ),
            )
            if not existed:
                count += 1
                _new_sale_notification(conn, line_id, legacy, li.get("title"), money(li.get("lineItemCost")),
                                       o.get("creationDate"), city or district)
                if legacy:
                    rule = conn.execute(
                        "SELECT bulk_buy_id, unit_cost FROM listing_rules WHERE legacy_item_id=?",
                        (legacy,),
                    ).fetchone()
                    if rule:
                        conn.execute(
                            "UPDATE sales SET bulk_buy_id=?, cost=? WHERE line_item_id=?",
                            (rule["bulk_buy_id"],
                             None if rule["unit_cost"] is None else rule["unit_cost"] * qty,
                             line_id),
                        )
        return count


def existed_status(conn, line_id):
    r = conn.execute("SELECT status FROM sales WHERE line_item_id=?", (line_id,)).fetchone()
    return r["status"] if r else None


def _recent(ts, days):
    d = _parse(ts)
    return bool(d and d > datetime.now(timezone.utc) - timedelta(days=days))


def _new_sale_notification(conn, line_id, legacy, title, price, sold_at, where):
    if not _recent(sold_at, 3):   # don't alert for history pulled in by a backfill
        return
    loc = None
    if legacy:
        r = conn.execute("""SELECT i.location FROM listings l JOIN items i ON i.id=l.item_id
                            WHERE l.legacy_item_id=?""", (legacy,)).fetchone()
        loc = r["location"] if r else None
    body = f"£{price:,.2f}" + (f" · to {where}" if where else "")
    body += f" · Stored at: {loc}" if loc else " · No storage location logged"
    notify(conn, "sale", f"Sold: {title}", body, f"/sales/{line_id}", key=f"sale-{line_id}", at=sold_at)
    if legacy:
        import crosslist
        crosslist.vinted_reminder_for_ebay_sale(conn, legacy, title, line_id, sold_at)


# --------------------------------------------------------------------------- #
# Listings & inventory
# --------------------------------------------------------------------------- #
def sync_listings(conn, listings) -> int:
    """Upsert active listings; anything previously active that's gone is marked ended/sold.
    Each listing gets an inventory item (created automatically if needed)."""
    now = now_iso()
    seen = set()
    n = 0
    listings = list(listings)          # download everything first, so the lock below only covers the writes
    with write_lock():
        for L in listings:
            lid = L["legacy_item_id"]
            if not lid:
                continue
            seen.add(lid)
            n += 1
            old = conn.execute("SELECT * FROM listings WHERE legacy_item_id=?", (lid,)).fetchone()
            if old is None:
                item_id = _find_or_create_item(conn, L)
                conn.execute(
                    """INSERT INTO listings(legacy_item_id, item_id, title, sku, price, original_price, currency,
                           quantity, quantity_available, quantity_sold, listing_type, duration, start_time,
                           end_time, status, watch_count, image_url, url, last_seen, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'active', ?,?,?,?,?)""",
                    (lid, item_id, L["title"], L["sku"], L["price"], L["price"], L["currency"], L["quantity"],
                     L["quantity_available"], L["quantity_sold"], L["listing_type"], L["duration"],
                     L["start_time"], L["end_time"], L["watch_count"], L["image_url"], L["url"], now, now))
                add_event(conn, item_id, "listed", f"Listed at £{(L['price'] or 0):,.2f} (#{lid})", lid,
                          at=L["start_time"])
            else:
                if old["price"] is not None and L["price"] is not None and abs(old["price"] - L["price"]) > 0.004:
                    add_event(conn, old["item_id"], "price",
                              f"Price changed £{old['price']:,.2f} → £{L['price']:,.2f}", lid)
                if old["title"] and L["title"] and old["title"] != L["title"]:
                    add_event(conn, old["item_id"], "title", f"Title changed to “{L['title']}”", lid)
                conn.execute(
                    """UPDATE listings SET title=?, sku=?, price=?, currency=?, quantity=?, quantity_available=?,
                           quantity_sold=?, listing_type=?, duration=?, start_time=?, end_time=?, status='active',
                           watch_count=?, image_url=?, url=?, last_seen=?, updated_at=? WHERE legacy_item_id=?""",
                    (L["title"], L["sku"], L["price"], L["currency"], L["quantity"], L["quantity_available"],
                     L["quantity_sold"], L["listing_type"], L["duration"], L["start_time"], L["end_time"],
                     L["watch_count"], L["image_url"], L["url"], now, now, lid))
        # listings no longer active
        for r in conn.execute("SELECT legacy_item_id, item_id FROM listings WHERE status='active'").fetchall():
            if r["legacy_item_id"] in seen:
                continue
            sold = conn.execute("SELECT 1 FROM sales WHERE legacy_item_id=?", (r["legacy_item_id"],)).fetchone()
            st = "sold" if sold else "ended"
            conn.execute("UPDATE listings SET status=?, updated_at=? WHERE legacy_item_id=?",
                         (st, now, r["legacy_item_id"]))
            add_event(conn, r["item_id"], st, "Listing sold out" if sold else "Listing ended",
                      r["legacy_item_id"])
        conn.commit()
    return n


def _find_or_create_item(conn, L) -> int:
    # an item the user logged with the same SKU and no listing yet
    if L.get("sku"):
        r = conn.execute("""SELECT id FROM items WHERE sku=? AND archived=0 AND id NOT IN
                            (SELECT item_id FROM listings WHERE item_id IS NOT NULL AND status='active')""",
                         (L["sku"],)).fetchone()
        if r:
            return r["id"]
    rule = conn.execute("SELECT bulk_buy_id, unit_cost FROM listing_rules WHERE legacy_item_id=?",
                        (L["legacy_item_id"],)).fetchone()
    cur = conn.execute(
        """INSERT INTO items(title, sku, quantity, cost, bulk_buy_id, purchased_at, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (L["title"], L["sku"], (L["quantity"] or 1), rule["unit_cost"] if rule else None,
         rule["bulk_buy_id"] if rule else None, None, now_iso(), now_iso()))
    add_event(conn, cur.lastrowid, "created", "Added from your eBay listings")
    return cur.lastrowid


def link_sales_to_items(conn) -> None:
    with write_lock():
        conn.execute("""UPDATE sales SET item_id = (SELECT item_id FROM listings l
                            WHERE l.legacy_item_id = sales.legacy_item_id)
                        WHERE item_id IS NULL AND legacy_item_id IN
                            (SELECT legacy_item_id FROM listings WHERE item_id IS NOT NULL)""")
        # sales of bulk items count towards that bulk buy
        conn.execute("""UPDATE sales SET bulk_buy_id = (SELECT bulk_buy_id FROM items i WHERE i.id = sales.item_id)
                        WHERE bulk_buy_id IS NULL AND item_id IN
                            (SELECT id FROM items WHERE bulk_buy_id IS NOT NULL)""")
        for r in conn.execute("""SELECT s.line_item_id, s.item_id, s.sold_at, s.title, s.quantity, s.item_price
                                 FROM sales s WHERE s.item_id IS NOT NULL AND NOT EXISTS
                                 (SELECT 1 FROM item_events e WHERE e.kind='sold' AND e.detail LIKE
                                  '%' || s.line_item_id || '%')""").fetchall():
            add_event(conn, r["item_id"], "sold",
                      f"Sold {r['quantity'] or 1} for £{(r['item_price'] or 0):,.2f} (line {r['line_item_id']})",
                      at=r["sold_at"])


def fill_dispatch_dates(conn, client, max_calls=400) -> int:
    """Record when each eBay order was dispatched. Uses eBay's shipping fulfilment record, falling back
    to the date of the eBay postage label."""
    rows = conn.execute("""SELECT DISTINCT s.order_id, o.raw FROM sales s JOIN orders o ON o.order_id = s.order_id
                           WHERE s.source='ebay' AND s.shipped_at IS NULL AND s.sold_at >= ?
                           ORDER BY s.sold_at DESC""", (_since(400),)).fetchall()
    n = 0
    for r in rows:
        try:
            status = json.loads(r["raw"] or "{}").get("orderFulfillmentStatus")
        except ValueError:
            status = None
        shipped = None
        if status in ("FULFILLED", "IN_PROGRESS") and n < max_calls and client is not None:
            n += 1
            try:
                shipped = client.shipped_date(r["order_id"])
            except EbayError:
                shipped = None
        if not shipped:
            lab = conn.execute("""SELECT MIN(date) FROM transactions WHERE order_id=? AND type='SHIPPING_LABEL'
                                  AND booking='DEBIT'""", (r["order_id"],)).fetchone()[0]
            shipped = lab
        if shipped:
            with write_lock():
                conn.execute("UPDATE sales SET shipped_at=? WHERE order_id=?", (shipped, r["order_id"]))
                conn.commit()                  # don't keep the database locked through the next eBay call
    return n


# --------------------------------------------------------------------------- #
# Error detection
# --------------------------------------------------------------------------- #
def detect_issues(conn) -> None:
    with write_lock():
        # duplicate postage label charges on one order
        for r in conn.execute("""SELECT order_id, COUNT(*) n, SUM(amount) total FROM transactions
                                 WHERE type='SHIPPING_LABEL' AND booking='DEBIT' AND order_id IS NOT NULL
                                   AND date >= ?
                                 GROUP BY order_id HAVING n > 1""", (_since(90),)).fetchall():
            notify(conn, "issue", "Possible duplicate postage label charge",
                   f"Order {r['order_id']} has {r['n']} label charges totalling £{r['total']:,.2f}. "
                   "Void any label you didn't use.", _order_link(conn, r["order_id"]),
                   key=f"duplabel-{r['order_id']}-{r['n']}")
        # label refunds / adjustments and fee credits
        for t in conn.execute("""SELECT * FROM transactions WHERE
                                   ((type='SHIPPING_LABEL' AND booking='CREDIT') OR type IN ('ADJUSTMENT', 'CREDIT'))
                                   AND date >= ?""", (_since(90),)).fetchall():
            if t["type"] == "SHIPPING_LABEL":
                title = "Postage label refunded or adjusted"
            elif t["type"] == "CREDIT":
                title = "eBay fee refund"
            else:
                title = "eBay adjustment"
            sign = "+" if t["booking"] == "CREDIT" else "-"
            body = f"{sign}£{(t['amount'] or 0):,.2f}" + (f" on order {t['order_id']}" if t["order_id"] else "")
            if t["memo"]:
                body += f" — {t['memo']}"
            notify(conn, "issue", title, body, _order_link(conn, t["order_id"]),
                   key=f"tx-{t['transaction_id']}-{t['type']}", at=t["date"])
        # cancellations / returns
        for r in conn.execute("""SELECT line_item_id, title, status, sold_at FROM sales
                                 WHERE status IN ('cancelled', 'refunded', 'part refunded') AND sold_at >= ?""",
                              (_since(90),)).fetchall():
            notify(conn, "issue", f"Order {r['status']}: {r['title']}",
                   "The refund and any fee credit have been applied to this sale's profit.",
                   f"/sales/{r['line_item_id']}", key=f"status-{r['line_item_id']}-{r['status']}")


def _since(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _order_link(conn, order_id):
    if not order_id:
        return ""
    r = conn.execute("SELECT line_item_id FROM sales WHERE order_id=? LIMIT 1", (order_id,)).fetchone()
    return f"/sales/{r['line_item_id']}" if r else ""


def _save_transaction(conn, t: dict) -> None:
    line_fees = {}
    for li in t.get("orderLineItems") or []:
        fees = [{"feeType": f.get("feeType"), "amount": money(f.get("amount"))}
                for f in (li.get("marketplaceFees") or [])]
        line_fees[li.get("lineItemId")] = {"fees": fees, "basis": money(li.get("feeBasisAmount"))}
    refs = t.get("references") or []
    order_id = t.get("orderId")
    if not order_id:
        for r in refs:
            if r.get("referenceType") == "ORDER_ID":
                order_id = r.get("referenceId")
                break
    if not order_id:
        # e.g. promoted-listing fee that only references the item: attach if unambiguous
        for r in refs:
            if r.get("referenceType") == "ITEM_ID":
                rows = conn.execute(
                    "SELECT DISTINCT order_id FROM sales WHERE legacy_item_id=?",
                    (r.get("referenceId"),),
                ).fetchall()
                if len(rows) == 1:
                    order_id = rows[0]["order_id"]
                break
    with write_lock():
        conn.execute(
            """INSERT OR REPLACE INTO transactions(transaction_id, type, order_id, booking, amount,
                   currency, fee_type, total_fee, fee_basis, memo, date, line_fees, refs, raw)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                t.get("transactionId"), t.get("transactionType"), order_id, t.get("bookingEntry"),
                abs(money(t.get("amount"))), currency(t.get("amount")), t.get("feeType"),
                money(t.get("totalFeeAmount")), money(t.get("totalFeeBasisAmount")),
                t.get("transactionMemo"), t.get("transactionDate"),
                json.dumps(line_fees), json.dumps(refs), dumps(t),
            ),
        )
