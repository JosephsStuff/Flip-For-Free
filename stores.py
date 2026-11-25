"""Other shops: CardTrader and Shopify.

* Connect each shop in Settings → Other shops (CardTrader API token; Shopify Dev Dashboard app keys).
* List an inventory item on CardTrader (matched to the right card in CardTrader's catalogue) or Shopify (built
  from its eBay listing), and change or remove those listings from the item.
* Each sync imports CardTrader and Shopify orders as sales, linked to the item they came from.
* Keep stock in step: when something sells anywhere (eBay, CardTrader, Shopify, or a sale you record), its
  quantity is taken off the item's listings on the other shops, once per sale.
"""
import difflib
import json
import re
from datetime import date, datetime, timedelta, timezone

from flask import abort, current_app, flash, redirect, render_template, request, url_for

import fx
import markets
from cardtrader_api import CardTraderClient, CardTraderError, money
from common import login_required, parse_float, parse_int
from db import Settings, add_event, connect, notify, now_iso, write_lock
from shopify_api import ShopifyClient, ShopifyError, clean_shop
from markets import money as fmt_money

CARD_GAMES_FIRST = ("yu-gi-oh", "yugioh", "pokemon", "pokémon", "magic", "one piece", "lorcana", "flesh")
CACHE_DAYS = {"games": 30, "expansions": 7, "categories": 30, "blueprints": 7}
ACTIVE_CT_STATES = {"paid", "sent", "arrived", "done", "hub_pending"}
SECRET_KEYS = ("ct_token", "shopify_client_secret")


def _demo() -> bool:
    try:
        return bool(current_app.config.get("DEMO"))
    except RuntimeError:
        return False


# --------------------------------------------------------------------------- #
# Connections
# --------------------------------------------------------------------------- #
def ct_client() -> CardTraderClient | None:
    tok = Settings().get("ct_token")
    return CardTraderClient(tok) if tok else None


def shopify_client() -> ShopifyClient | None:
    s = Settings().get_many(["shopify_shop", "shopify_client_id", "shopify_client_secret", "shopify_api_version"])
    if not (s.get("shopify_shop") and s.get("shopify_client_id") and s.get("shopify_client_secret")):
        return None
    return ShopifyClient(s["shopify_shop"], s["shopify_client_id"], s["shopify_client_secret"],
                         s.get("shopify_api_version") or None)


def connections() -> dict:
    s = Settings().get_many(["ct_token", "ct_account", "shopify_shop", "shopify_client_secret", "shopify_name",
                             "shopify_location_id", "shopify_location_name", "shopify_currency",
                             "shopify_fee_pct", "shopify_fee_fixed", "stock_sync_auto", "stores_last_sync",
                             "stores_last_result"])
    return {
        "cardtrader": bool(s.get("ct_token")), "ct_account": s.get("ct_account"),
        "shopify": bool(s.get("shopify_shop") and s.get("shopify_client_secret")),
        "shopify_shop": s.get("shopify_shop"), "shopify_name": s.get("shopify_name"),
        "shopify_location": s.get("shopify_location_name"), "shopify_location_id": s.get("shopify_location_id"),
        "shopify_currency": s.get("shopify_currency") or "GBP",
        "shopify_fee_pct": s.get("shopify_fee_pct") or "", "shopify_fee_fixed": s.get("shopify_fee_fixed") or "",
        "stock_sync_auto": (s.get("stock_sync_auto") or "1") == "1",
        "last_sync": s.get("stores_last_sync"), "last_result": s.get("stores_last_result"),
    }


# --------------------------------------------------------------------------- #
# CardTrader catalogue (cached)
# --------------------------------------------------------------------------- #
def _cached(conn, key, kind, fetch):
    row = conn.execute("SELECT data, fetched_at FROM ct_cache WHERE key=?", (key,)).fetchone()
    if row:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(row["fetched_at"].replace("Z", "+00:00"))
            if age < timedelta(days=CACHE_DAYS[kind]):
                return json.loads(row["data"])
        except ValueError:
            pass
    data = fetch()
    with write_lock():
        conn.execute("INSERT INTO ct_cache(key, data, fetched_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE "
                     "SET data=excluded.data, fetched_at=excluded.fetched_at", (key, json.dumps(data), now_iso()))
        conn.commit()
    return data


def ct_games(conn, client):
    games = _cached(conn, "games", "games", client.games)
    if isinstance(games, dict):
        games = games.get("array") or []

    def rank(g):
        n = (g.get("display_name") or g.get("name") or "").lower()
        return next((i for i, k in enumerate(CARD_GAMES_FIRST) if k in n), 99), n
    return sorted(games, key=rank)


def ct_expansions(conn, client, game_id):
    allx = _cached(conn, "expansions", "expansions", client.expansions)
    return sorted([x for x in allx if x.get("game_id") == game_id], key=lambda x: x.get("name") or "")


def ct_blueprints(conn, client, expansion_id):
    return _cached(conn, f"bp:{expansion_id}", "blueprints", lambda: client.blueprints(expansion_id))


def ct_category(conn, client, game_id, category_id):
    cats = _cached(conn, f"cat:{game_id}", "categories", lambda: client.categories(game_id))
    return next((c for c in cats if c.get("id") == category_id), None)


SET_CODE = re.compile(r"\b([A-Z0-9]{2,5})-(?:[A-Z]{1,2})?(\d{2,4})\b")


def guess_set_code(title: str) -> tuple[str | None, str | None]:
    """'Dark Magician SDY-006 Ultra Rare' -> ('SDY', '006'); 'LOB-EN124' -> ('LOB', '124')."""
    m = SET_CODE.search((title or "").upper())
    return (m.group(1), m.group(2)) if m else (None, None)


def _norm(s):
    return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())


def rank_blueprints(blueprints, title, number=None, query=None, limit=12):
    """Best matches for an item title (and optional card number / typed search) among a set's cards."""
    t = _norm(query or title)
    out = []
    for b in blueprints:
        name = _norm(b.get("name"))
        score = difflib.SequenceMatcher(None, name, t).ratio()
        if name and name in t:
            score += 0.6
        if query and _norm(query) in name:
            score += 0.8
        num = str((b.get("fixed_properties") or {}).get("collector_number") or "")
        if number and num and num.lstrip("0").endswith(number.lstrip("0")):
            score += 1.0
        out.append((score, b))
    out.sort(key=lambda x: -x[0])
    return [b for s, b in out[:limit]]


# --------------------------------------------------------------------------- #
# Stock in step
# --------------------------------------------------------------------------- #
def item_channel_listings(conn, item_id, channel=None):
    q = "SELECT * FROM channel_listings WHERE item_id=? AND status='active'"
    args = [item_id]
    if channel:
        q += " AND channel=?"
        args.append(channel)
    return [dict(r) for r in conn.execute(q, args)]


def _take_from_channel_listing(conn, L, take, sold_on):
    """Lower one CardTrader/Shopify listing by `take`. Returns a short description."""
    if L["channel"] == "CardTrader":
        client = ct_client()
        if not client:
            return f"CardTrader isn't connected, so listing {L['external_id']} wasn't changed"
        client.increment(L["external_id"], -take)             # CardTrader deletes it at 0
        left = max((L["quantity"] or 0) - take, 0)
    else:
        client = shopify_client()
        loc = Settings().get("shopify_location_id")
        if not client or not loc:
            return "Shopify isn't connected, so its stock wasn't changed"
        left = max(client.adjust(L["inventory_item_id"], loc, -take), 0)
    with write_lock():
        conn.execute("UPDATE channel_listings SET quantity=?, status=?, updated_at=? WHERE id=?",
                     (left, "active" if left > 0 else "sold out", now_iso(), L["id"]))
        add_event(conn, L["item_id"], L["channel"].lower(),
                  f"Sold on {sold_on}: {L['channel']} now has {left} left")
        conn.commit()
    return f"{L['channel']} now has {left} left"


def propagate_sale(conn, line_item_id, item_id, quantity, sold_on, include_ebay=True, sold_at=None) -> str:
    """Take a sale's quantity off the item's listings on every other shop. Runs once per sale."""
    if not item_id:
        return ""
    if conn.execute("SELECT 1 FROM stock_sync_done WHERE line_item_id=?", (line_item_id,)).fetchone():
        return ""
    notes, problems = [], []
    if include_ebay and sold_on != "eBay":
        import crosslist
        msg = crosslist.take_off_ebay(conn, item_id, quantity, sold_on)
        if msg:
            (problems if "couldn't" in msg.lower() else notes).append(msg.removeprefix("Also ").rstrip("."))
    remaining = quantity
    for L in item_channel_listings(conn, item_id):
        if L["channel"] == sold_on or remaining <= 0:
            continue
        if sold_at and (L.get("created_at") or "") > sold_at:
            continue                     # listed there after this sale happened: its stock is already right
        take = min(remaining, L["quantity"] or 0) or remaining
        if _demo():
            notes.append(f"demo: {L['channel']} would drop by {take}")
            continue
        try:
            notes.append(_take_from_channel_listing(conn, L, take, sold_on))
        except (CardTraderError, ShopifyError) as e:
            problems.append(f"{L['channel']}: {e}")
            with write_lock():
                notify(conn, "issue", f"Couldn't update {L['channel']} after a sale",
                       f"{e}. Check the listing so it isn't sold twice.", f"/inventory/{item_id}")
                conn.commit()
    result = "; ".join(notes + problems)
    with write_lock():
        conn.execute("INSERT OR IGNORE INTO stock_sync_done(line_item_id, at, result) VALUES (?,?,?)",
                     (line_item_id, now_iso(), result))
        conn.commit()
    return result


def propagate_recent_sales(conn, days=3) -> int:
    """After a sync: new sales (any shop) of items listed elsewhere take that stock off the other shops."""
    if Settings().get("stock_sync_auto", "1") != "1":
        return 0
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        """SELECT s.line_item_id, s.item_id, s.quantity, s.sold_at, COALESCE(s.channel, 'eBay') AS channel FROM sales s
           WHERE s.item_id IS NOT NULL AND s.source IN ('ebay', 'cardtrader', 'shopify') AND s.sold_at >= ?
             AND COALESCE(s.status, '') NOT IN ('cancelled', 'refunded')
             AND NOT EXISTS (SELECT 1 FROM stock_sync_done d WHERE d.line_item_id = s.line_item_id)
             AND (EXISTS (SELECT 1 FROM channel_listings c WHERE c.item_id = s.item_id AND c.status = 'active'
                          AND c.created_at <= s.sold_at)   -- a sale from before it was listed there doesn't count
                  OR (s.source <> 'ebay' AND EXISTS (SELECT 1 FROM listings l WHERE l.item_id = s.item_id
                                                     AND l.status = 'active')))""", (since,)).fetchall()
    for r in rows:
        propagate_sale(conn, r["line_item_id"], r["item_id"], r["quantity"] or 1, r["channel"], sold_at=r["sold_at"])
    return len(rows)


# --------------------------------------------------------------------------- #
# Orders -> sales
# --------------------------------------------------------------------------- #
def _upsert_sale(conn, vals: dict) -> bool:
    """Insert a new external sale, or only refresh its status if it's already there (your edits are kept)."""
    exists = conn.execute("SELECT 1 FROM sales WHERE line_item_id=?", (vals["line_item_id"],)).fetchone()
    with write_lock():
        if exists:
            conn.execute("UPDATE sales SET status=?, updated_at=? WHERE line_item_id=?",
                         (vals["status"], now_iso(), vals["line_item_id"]))
        else:
            bulk = None
            if vals.get("item_id"):
                r = conn.execute("SELECT bulk_buy_id FROM items WHERE id=?", (vals["item_id"],)).fetchone()
                bulk = r["bulk_buy_id"] if r else None
            conn.execute(
                """INSERT INTO sales(line_item_id, order_id, title, quantity, item_price, delivery_charged, manual_fees,
                       channel, sold_at, item_id, bulk_buy_id, status, notes, source, currency, ship_country, ship_city,
                       updated_at)
                   VALUES (:line_item_id, :order_id, :title, :quantity, :item_price, :delivery_charged, :manual_fees,
                       :channel, :sold_at, :item_id, :bulk, :status, :notes, :source, 'GBP', :ship_country,
                       :ship_city, :now)""", {**vals, "bulk": bulk, "now": now_iso(),
                                              "ship_country": vals.get("ship_country"),
                                              "ship_city": vals.get("ship_city")})
            if vals.get("item_id"):
                add_event(conn, vals["item_id"], "sold",
                          f"Sold {vals['quantity']} on {vals['channel']} for {fmt_money(vals['item_price'])}",
                          at=vals["sold_at"])
            if vals["sold_at"] >= (datetime.now(timezone.utc) - timedelta(days=3)).isoformat():
                notify(conn, "sale", f"Sold on {vals['channel']}: {vals['title']}", f"{fmt_money(vals['item_price'])}",
                       "/sales", key=f"sale-{vals['line_item_id']}", at=vals["sold_at"])
        conn.commit()
    return not exists


def _since(key, first_days):
    last = Settings().get(key)
    if last:
        return (datetime.fromisoformat(last.replace("Z", "+00:00")) - timedelta(days=30)).date()
    return date.today() - timedelta(days=first_days)


def import_cardtrader(conn, client) -> int:
    first_days = int(Settings().get("first_sync_days") or 730)
    since = _since("ct_orders_synced", first_days)
    new = 0
    for o in client.iter_orders(since.isoformat()):
        state = o.get("state") or ""
        status = "cancelled" if state in ("canceled", "request_for_cancel") else state
        sold_at = o.get("paid_at") or o.get("sent_at") or o.get("credit_added_to_seller_at") or now_iso()
        items = o.get("order_items") or []
        sub, cur = money(o.get("seller_subtotal"))
        total, _ = money(o.get("seller_total"))
        fee, _ = money(o.get("seller_fee_amount"))
        # seller_total = items + postage - CardTrader's fee, so postage is what's left over
        shipping = max(round(total - sub + fee, 2), 0.0) if total else 0.0
        lines = []
        for it in items:
            unit, icur = money(it.get("seller_price"))
            lines.append((it, unit * (it.get("quantity") or 1), icur or cur))
        items_total = sum(x[1] for x in lines) or 1
        for n, (it, price, icur) in enumerate(lines):
            share = price / items_total
            pid = str(it.get("product_id") or "")
            link = conn.execute("SELECT item_id FROM channel_listings WHERE channel='CardTrader' AND external_id=?",
                                (pid,)).fetchone()
            name = it.get("name") or "CardTrader sale"
            if it.get("expansion"):
                exp = it["expansion"].get("name") if isinstance(it["expansion"], dict) else it["expansion"]
                name = f"{name} ({exp})" if exp else name
            new += _upsert_sale(conn, {
                "line_item_id": f"CT-{it.get('id')}", "order_id": f"CT-{o.get('id')}", "title": name,
                "quantity": it.get("quantity") or 1, "item_price": fx.to_home(price, icur),
                "delivery_charged": fx.to_home(shipping, cur) if n == 0 else 0.0,
                "manual_fees": fx.to_home(fee * share, cur), "channel": "CardTrader",
                "sold_at": sold_at, "item_id": link["item_id"] if link else None, "status": status,
                "source": "cardtrader",
                "notes": f"CardTrader order {o.get('code') or o.get('id')}"
                         + (f" ({cur}: items {sub:.2f}, total {total:.2f}, fee {fee:.2f})" if cur and cur != "GBP"
                            else "")})
    Settings().set("ct_orders_synced", now_iso())
    return new


def import_shopify(conn, client) -> int:
    first_days = int(Settings().get("first_sync_days") or 730)
    since = _since("shopify_orders_synced", first_days)
    s = Settings().get_many(["shopify_fee_pct", "shopify_fee_fixed"])
    pct, fixed = (parse_float(s.get("shopify_fee_pct")) or 0) / 100, parse_float(s.get("shopify_fee_fixed")) or 0
    new = 0
    for o in client.iter_orders(since.isoformat()):
        fin = (o.get("displayFinancialStatus") or "").upper()
        status = "cancelled" if o.get("cancelledAt") else ("refunded" if fin == "REFUNDED" else fin.lower() or None)
        ship_m = ((o.get("totalShippingPriceSet") or {}).get("shopMoney") or {})
        shipping, scur = float(ship_m.get("amount") or 0), ship_m.get("currencyCode")
        addr = o.get("shippingAddress") or {}
        lines = (o.get("lineItems") or {}).get("nodes") or []
        totals = [float(((li.get("discountedTotalSet") or {}).get("shopMoney") or {}).get("amount") or 0)
                  for li in lines]
        order_total = sum(totals) + shipping
        for n, li in enumerate(lines):
            m = (li.get("discountedTotalSet") or {}).get("shopMoney") or {}
            price, cur = float(m.get("amount") or 0), m.get("currencyCode")
            gross = price + (shipping if n == 0 else 0)
            fee = (order_total * pct + fixed) * (gross / order_total) if order_total else 0
            pid = (li.get("product") or {}).get("id")
            link = conn.execute("SELECT item_id FROM channel_listings WHERE channel='Shopify' AND external_id=?",
                                (pid,)).fetchone() if pid else None
            new += _upsert_sale(conn, {
                "line_item_id": "SH-" + li["id"].rsplit("/", 1)[-1], "order_id": "SH-" + o["id"].rsplit("/", 1)[-1],
                "title": li.get("title") or "Shopify sale", "quantity": li.get("quantity") or 1,
                "item_price": fx.to_home(price, cur), "delivery_charged": fx.to_home(shipping, scur) if n == 0 else 0.0,
                "manual_fees": fx.to_home(fee, cur), "channel": "Shopify",
                "sold_at": o.get("processedAt") or o.get("createdAt"), "item_id": link["item_id"] if link else None,
                "status": status, "source": "shopify", "notes": f"Shopify order {o.get('name')}",
                "ship_country": addr.get("countryCodeV2"), "ship_city": addr.get("city")})
    Settings().set("shopify_orders_synced", now_iso())
    return new


def sync_stores(conn) -> str:
    """Import orders from connected shops, then keep stock in step. Returns a summary for the sync log."""
    parts, errs = [], []
    if _demo():
        return ""
    ct, sh = None, None
    try:
        ct = ct_client()
        if ct:
            parts.append(f"{import_cardtrader(conn, ct)} new CardTrader sales")
    except (CardTraderError, fx.FxError) as e:
        errs.append(f"CardTrader: {e}")
    try:
        sh = shopify_client()
        if sh:
            parts.append(f"{import_shopify(conn, sh)} new Shopify sales")
    except (ShopifyError, fx.FxError) as e:
        errs.append(f"Shopify: {e}")
    if ct or sh or conn.execute("SELECT 1 FROM listings WHERE status='active' LIMIT 1").fetchone():
        try:
            from sync import link_sales_to_items
            link_sales_to_items(conn)
        except Exception:  # noqa: BLE001
            pass
        propagate_recent_sales(conn)
    for e in errs:
        with write_lock():
            notify(conn, "issue", "Other shops didn't sync", e, "/settings/shops",
                   key=f"stores-err-{date.today()}-{e[:40]}")
            conn.commit()
    summary = ", ".join(parts + errs)
    if ct or sh:
        Settings().set_many({"stores_last_sync": now_iso(), "stores_last_result": summary or "Nothing new"})
    return summary


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
def _description_html(details) -> str:
    import crosslist
    from html import escape
    text = crosslist.vinted_description(details) if details else ""
    paras = [p for p in text.split("\n\n") if p.strip()]
    out = []
    for p in paras:
        lines = p.split("\n")
        if all(ln.startswith("• ") for ln in lines):
            out.append("<ul>" + "".join(f"<li>{escape(ln[2:])}</li>" for ln in lines) + "</ul>")
        else:
            out.append("<p>" + "<br>".join(escape(ln) for ln in lines) + "</p>")
    return "\n".join(out)


def _item(conn, item_id):
    row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not row:
        abort(404)
    from features import load_items
    return load_items(conn, "i.id = ?", (item_id,))[0]


def _stock_left(it) -> int:
    return max((it.get("quantity") or 1) - (it.get("sold_units") or 0), 0)


def register(app):
    @app.route("/settings/shops", methods=["GET", "POST"])
    @login_required
    def shops_settings():
        s = Settings()
        if request.method == "POST":
            act = request.form.get("action")
            try:
                if act == "ct_connect":
                    tok = (request.form.get("ct_token") or "").strip()
                    info = CardTraderClient(tok).info()
                    s.set_many({"ct_token": tok, "ct_account": info.get("name") or "connected"})
                    flash(f"CardTrader connected ({info.get('name')}).", "ok")
                elif act == "ct_disconnect":
                    s.set_many({"ct_token": None, "ct_account": None})
                    flash("CardTrader disconnected. Your listings there are unchanged.", "ok")
                elif act == "shopify_connect":
                    shop = clean_shop(request.form.get("shopify_shop"))
                    cid = (request.form.get("shopify_client_id") or "").strip()
                    sec = (request.form.get("shopify_client_secret") or "").strip() or s.get("shopify_client_secret")
                    info = ShopifyClient(shop, cid, sec).shop_info()
                    loc = info["locations"][0] if info["locations"] else None
                    s.set_many({"shopify_shop": shop, "shopify_client_id": cid, "shopify_client_secret": sec,
                                "shopify_name": info["name"], "shopify_currency": info["currency"],
                                "shopify_location_id": loc["id"] if loc else None,
                                "shopify_location_name": loc["name"] if loc else None})
                    flash(f"Shopify connected: {info['name']}" + (f", stock at {loc['name']}." if loc else
                          ". No active location found, so stock can't be set yet."), "ok")
                elif act == "shopify_location":
                    client = shopify_client()
                    locs = {x["id"]: x["name"] for x in client.shop_info()["locations"]}
                    lid = request.form.get("location_id")
                    if lid in locs:
                        s.set_many({"shopify_location_id": lid, "shopify_location_name": locs[lid]})
                elif act == "shopify_disconnect":
                    s.set_many({k: None for k in ("shopify_shop", "shopify_client_id", "shopify_client_secret",
                                                  "shopify_name", "shopify_location_id", "shopify_location_name")})
                    flash("Shopify disconnected. Your products there are unchanged.", "ok")
                elif act == "prefs":
                    s.set_many({"stock_sync_auto": "1" if request.form.get("stock_sync_auto") else "0",
                                "shopify_fee_pct": parse_float(request.form.get("shopify_fee_pct")) or "",
                                "shopify_fee_fixed": parse_float(request.form.get("shopify_fee_fixed")) or ""})
                    flash("Saved.", "ok")
                elif act == "sync":
                    conn = connect()
                    try:
                        flash("Shops synced: " + (sync_stores(conn) or "nothing new") + ".", "ok")
                    finally:
                        conn.close()
            except (CardTraderError, ShopifyError, fx.FxError) as e:
                flash(str(e), "warn")
            return redirect(url_for("shops_settings"))
        c = connections()
        locations = []
        if c["shopify"] and request.args.get("locations"):
            try:
                locations = shopify_client().shop_info()["locations"]
            except ShopifyError as e:
                flash(str(e), "warn")
        conn = connect()
        try:
            counts = {r["channel"]: r["n"] for r in conn.execute(
                "SELECT channel, COUNT(*) n FROM channel_listings WHERE status='active' GROUP BY channel")}
        finally:
            conn.close()
        return render_template("shops.html", c=c, counts=counts, locations=locations,
                               scopes=", ".join(__import__("shopify_api").SCOPES_NEEDED))

    # ------------------------------------------------------------------ CardTrader
    @app.route("/inventory/<int:item_id>/cardtrader", methods=["GET", "POST"])
    @login_required
    def cardtrader_item(item_id):
        client = ct_client()
        conn = connect()
        try:
            it = _item(conn, item_id)
            if request.method == "POST":
                if not client:
                    return redirect(url_for("shops_settings"))
                act = request.form.get("action")
                try:
                    if act == "create":
                        bp = parse_int(request.form.get("blueprint_id"))
                        price = parse_float(request.form.get("price"))
                        qty = parse_int(request.form.get("quantity")) or 1
                        if not bp or not price or price <= 0:
                            raise CardTraderError("Choose the card and enter a price.")
                        props = {}
                        for k, v in request.form.items():
                            if k.startswith("prop_") and v != "":
                                props[k[5:]] = {"true": True, "false": False}.get(v, v)
                        res = client.create_product(bp, price, qty, props or None,
                                                    (request.form.get("description") or "").strip() or None,
                                                    user_data=f"flip:{item_id}")
                        prod = res.get("resource") or res
                        pid = str(prod.get("id"))
                        cents, cur = money(prod.get("price"))
                        with write_lock():
                            conn.execute(
                                """INSERT INTO channel_listings(item_id, channel, external_id, blueprint_id, title,
                                       quantity, price, currency, url, status, created_at, updated_at)
                                   VALUES (?, 'CardTrader', ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                                   ON CONFLICT(channel, external_id) DO UPDATE SET quantity=excluded.quantity,
                                       price=excluded.price, status='active', updated_at=excluded.updated_at""",
                                (item_id, pid, bp, prod.get("name_en") or request.form.get("card_name") or it["title"],
                                 qty, cents or price, cur or request.form.get("currency"), None, now_iso(),
                                 now_iso()))
                            add_event(conn, item_id, "cardtrader", f"Listed {qty} on CardTrader at {price:.2f} "
                                      f"{cur or request.form.get('currency') or ''}".strip())
                            conn.commit()
                        warns = res.get("warnings") or {}
                        flash("Listed on CardTrader." + (f" CardTrader noted: {warns}" if warns else ""), "ok")
                    elif act in ("update", "remove"):
                        L = conn.execute("SELECT * FROM channel_listings WHERE id=? AND item_id=? AND "
                                         "channel='CardTrader'", (parse_int(request.form.get("listing_id")),
                                                                  item_id)).fetchone()
                        if not L:
                            abort(404)
                        if act == "remove":
                            try:
                                client.delete_product(L["external_id"])
                            except CardTraderError as e:
                                if e.status != 404:
                                    raise
                            status, msg = "removed", "Removed from CardTrader."
                            new_q, new_p = L["quantity"], L["price"]
                        else:
                            new_p = parse_float(request.form.get("price")) or L["price"]
                            new_q = parse_int(request.form.get("quantity"))
                            new_q = L["quantity"] if new_q is None else new_q
                            if new_q <= 0:
                                client.delete_product(L["external_id"])
                                status, msg = "removed", "Quantity 0: removed from CardTrader."
                            else:
                                client.update_product(L["external_id"], price=round(new_p, 2), quantity=new_q)
                                status, msg = "active", "CardTrader listing updated."
                        with write_lock():
                            conn.execute("UPDATE channel_listings SET price=?, quantity=?, status=?, updated_at=? "
                                         "WHERE id=?", (new_p, new_q, status, now_iso(), L["id"]))
                            add_event(conn, item_id, "cardtrader", msg.rstrip("."))
                            conn.commit()
                        flash(msg, "ok")
                except (CardTraderError, fx.FxError) as e:
                    flash(str(e), "warn")
                return redirect(url_for("cardtrader_item", item_id=item_id))

            ctx = {"it": it, "connected": bool(client), "listings": item_channel_listings(conn, item_id, "CardTrader"),
                   "stock_left": _stock_left(it), "error": None}
            if not client:
                return render_template("cardtrader.html", **ctx)
            try:
                games = ct_games(conn, client)
                game_id = parse_int(request.args.get("game")) or (games[0]["id"] if games else None)
                code, number = guess_set_code(it["title"])
                exps = ct_expansions(conn, client, game_id) if game_id else []
                exp_id = parse_int(request.args.get("expansion"))
                if not exp_id and code:
                    exp_id = next((x["id"] for x in exps if (x.get("code") or "").upper() == code), None)
                q = (request.args.get("q") or "").strip()
                candidates = []
                if exp_id:
                    candidates = rank_blueprints(ct_blueprints(conn, client, exp_id), it["title"], number, q)
                bp_id = parse_int(request.args.get("blueprint"))
                chosen, props, market, currency = None, [], [], None
                if bp_id and exp_id:
                    chosen = next((b for b in ct_blueprints(conn, client, exp_id) if b.get("id") == bp_id), None)
                if chosen:
                    cat = ct_category(conn, client, chosen.get("game_id"), chosen.get("category_id")) or {}
                    editable = chosen.get("editable_properties") or []
                    names = {p.get("name") for p in editable} if editable else None
                    props = [p for p in cat.get("properties") or [] if names is None or p.get("name") in names]
                    market = client.marketplace(chosen["id"])[:10]
                    currency = next((money(p.get("price"))[1] for p in market if p.get("price")), None)
                listing_price = it.get("list_price")
                suggested = None
                if listing_price and currency:
                    try:
                        suggested = fx.convert(listing_price, markets.currency(), currency)
                    except fx.FxError:
                        suggested = None
                ctx.update(games=games, game_id=game_id, expansions=exps, expansion_id=exp_id, code=code,
                           number=number, q=q, candidates=candidates, chosen=chosen, props=props,
                           market=[{"price": money(p.get("price"))[0], "currency": money(p.get("price"))[1],
                                    "qty": p.get("quantity"), "props": p.get("properties_hash") or {},
                                    "seller": (p.get("user") or {}).get("username")} for p in market],
                           currency=currency, suggested=suggested, ebay_price=listing_price)
            except (CardTraderError, fx.FxError) as e:
                ctx["error"] = str(e)
        finally:
            conn.close()
        return render_template("cardtrader.html", **ctx)

    # ------------------------------------------------------------------ Shopify
    @app.route("/inventory/<int:item_id>/shopify", methods=["GET", "POST"])
    @login_required
    def shopify_item(item_id):
        client = shopify_client()
        conn = connect()
        try:
            it = _item(conn, item_id)
            c = connections()
            if request.method == "POST":
                if not client:
                    return redirect(url_for("shops_settings"))
                act = request.form.get("action")
                try:
                    if act == "create":
                        if not c["shopify_location_id"]:
                            raise ShopifyError("Choose a Shopify stock location in Settings → Other shops first.")
                        price = parse_float(request.form.get("price"))
                        if not price or price <= 0:
                            raise ShopifyError("Enter a price.")
                        qty = parse_int(request.form.get("quantity")) or 1
                        images = request.form.getlist("image")
                        status = "DRAFT" if request.form.get("draft") else "ACTIVE"
                        res = client.create_product(
                            title=(request.form.get("title") or it["title"]).strip(),
                            description_html=request.form.get("description_html") or "", price=price, quantity=qty,
                            location_id=c["shopify_location_id"], sku=(request.form.get("sku") or "").strip() or None,
                            images=images, status=status)
                        published = client.publish_online_store(res["product_id"]) if status == "ACTIVE" else False
                        domain = c["shopify_shop"]
                        with write_lock():
                            conn.execute(
                                """INSERT INTO channel_listings(item_id, channel, external_id, variant_id,
                                       inventory_item_id, title, quantity, price, currency, url, status, created_at,
                                       updated_at) VALUES (?, 'Shopify', ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
                                (item_id, res["product_id"], res["variant_id"], res["inventory_item_id"],
                                 request.form.get("title") or it["title"], qty, price, c["shopify_currency"],
                                 res.get("url") or f"https://{domain}/admin/products/"
                                 + res["product_id"].rsplit("/", 1)[-1], now_iso(), now_iso()))
                            add_event(conn, item_id, "shopify", f"Added to Shopify ({status.lower()}) at "
                                      f"{price:.2f} {c['shopify_currency']}")
                            conn.commit()
                        flash("Added to Shopify" + (" and published to your Online Store." if published else
                              " as a draft." if status == "DRAFT" else ". Choose its sales channels in Shopify if "
                              "it isn't showing in your store."), "ok")
                    elif act in ("update", "remove", "draft", "activate"):
                        L = conn.execute("SELECT * FROM channel_listings WHERE id=? AND item_id=? AND "
                                         "channel='Shopify'", (parse_int(request.form.get("listing_id")),
                                                               item_id)).fetchone()
                        if not L:
                            abort(404)
                        status = L["status"]
                        if act == "remove":
                            client.delete_product(L["external_id"])
                            status, msg = "removed", "Deleted from Shopify."
                        elif act == "draft":
                            client.set_status(L["external_id"], "DRAFT")
                            status, msg = "draft", "Hidden on Shopify (draft)."
                        elif act == "activate":
                            client.set_status(L["external_id"], "ACTIVE")
                            status, msg = "active", "Shopify product is live again."
                        else:
                            new_p = parse_float(request.form.get("price"))
                            if new_p and new_p != L["price"]:
                                client.set_price(L["external_id"], L["variant_id"], new_p)
                            new_q = parse_int(request.form.get("quantity"))
                            if new_q is not None and c["shopify_location_id"]:
                                cur = client.available(L["inventory_item_id"], c["shopify_location_id"])
                                if new_q != cur:
                                    client.adjust(L["inventory_item_id"], c["shopify_location_id"], new_q - cur)
                            with write_lock():
                                conn.execute("UPDATE channel_listings SET price=COALESCE(?, price), "
                                             "quantity=COALESCE(?, quantity), updated_at=? WHERE id=?",
                                             (new_p, new_q, now_iso(), L["id"]))
                                conn.commit()
                            msg = "Shopify product updated."
                        with write_lock():
                            conn.execute("UPDATE channel_listings SET status=?, updated_at=? WHERE id=?",
                                         (status, now_iso(), L["id"]))
                            add_event(conn, item_id, "shopify", msg.rstrip("."))
                            conn.commit()
                        flash(msg, "ok")
                except ShopifyError as e:
                    flash(str(e), "warn")
                return redirect(url_for("shopify_item", item_id=item_id))

            import crosslist
            listing = crosslist._source_listing(conn, item_id)
            details, error = None, None
            if listing:
                try:
                    details = crosslist.listing_details(conn, listing)
                except Exception as e:  # noqa: BLE001
                    error = str(e)
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM channel_listings WHERE item_id=? AND channel='Shopify' AND status <> 'removed'",
                (item_id,))]
            ctx = dict(it=it, connected=bool(client), c=c, listings=rows, error=error,
                       title=(details or {}).get("title") or it["title"],
                       description_html=_description_html(details), pictures=(details or {}).get("pictures", []),
                       price=(details or {}).get("price") or it.get("list_price") or "",
                       stock_left=_stock_left(it))
        finally:
            conn.close()
        return render_template("shopify.html", **ctx)
