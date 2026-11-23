"""Cross-listing helpers: copy an eBay listing to Vinted by hand, and keep eBay in step with sales made elsewhere.

Vinted only allows its own Pro API for automated listing (allowlisted sellers), and its terms forbid other tools
acting on your account. So nothing here touches Vinted: Flip for Free prepares the text, price and photos, you paste
them into Vinted yourself, and you tell Flip it's listed there. eBay changes use eBay's official API.
"""
import io
import json
import re
import zipfile
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlparse

import requests
from flask import Response, abort, current_app, flash, redirect, render_template, request, url_for

import profit
from common import login_required, parse_float
from db import Settings, add_event, connect, notify, now_iso, write_lock

DETAILS_MAX_AGE = timedelta(hours=12)
EBAY_IMAGE_HOSTS = (".ebayimg.com",)

# eBay condition IDs -> the closest Vinted condition
VINTED_CONDITIONS = ["New with tags", "New without tags", "Very good", "Good", "Satisfactory"]
_COND = {"1000": "New with tags", "1500": "New without tags", "1750": "New without tags",
         "2000": "Very good", "2010": "Very good", "2020": "Very good", "2030": "Very good", "2500": "Very good",
         "2750": "Very good", "2990": "Very good", "3000": "Good", "3010": "Good", "4000": "Very good",
         "5000": "Good", "6000": "Satisfactory", "7000": "Satisfactory"}


def _demo() -> bool:
    try:
        return bool(current_app.config.get("DEMO"))
    except RuntimeError:                       # background sync: no app context, never demo
        return False


# --------------------------------------------------------------------------- #
# Text
# --------------------------------------------------------------------------- #
class _Text(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table", "section"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag == "li":
            self.out.append("\n• ")
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(self.skip - 1, 0)
        elif tag in self.BLOCK and tag != "li":
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_to_text(html: str) -> str:
    p = _Text()
    p.feed(html or "")
    text = unescape("".join(p.out)).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def vinted_description(details: dict) -> str:
    parts = []
    body = html_to_text(details.get("description_html", ""))
    if body:
        parts.append(body)
    if details.get("condition_note"):
        parts.append("Condition: " + details["condition_note"].strip())
    useful = [(k, v) for k, v in details.get("specifics", []) if v and v.lower() not in ("does not apply", "n/a", "-")]
    if useful:
        parts.append("\n".join(f"{k}: {v}" for k, v in useful[:15]))
    return "\n\n".join(parts).strip()


# --------------------------------------------------------------------------- #
# Price
# --------------------------------------------------------------------------- #
def ebay_fee_rate(conn) -> tuple[float, str]:
    """Share of the buyer's payment eBay took on your last 180 days of eBay sales (13% if there's too little data)."""
    since = (datetime.now(timezone.utc) - timedelta(days=180)).isoformat()
    sales = profit.compute_sales(conn, "COALESCE(s.channel, 'eBay') = 'eBay' AND s.sold_at >= ?", (since,))
    tot = profit.summarise(sales)
    if tot["count"] >= 5 and tot["gross"] > 0:
        return tot["ebay"] / tot["gross"], f"your eBay fees over the last 180 days ({tot['count']} sales)"
    return 0.13, "a typical eBay rate (not enough of your own sales yet)"


def price_suggestion(conn, ebay_price: float) -> dict:
    rate, basis = ebay_fee_rate(conn)
    vinted_fee = (parse_float(Settings().get("vinted_fee_pct")) or 0.0) / 100
    take_home = ebay_price * (1 - rate)
    same = take_home / (1 - vinted_fee) if vinted_fee < 1 else take_home
    return {"ebay_price": ebay_price, "ebay_rate": rate, "basis": basis, "ebay_take_home": take_home,
            "vinted_fee_pct": vinted_fee * 100, "same_take_home": round(same, 2),
            "match_take_home": round(ebay_price * (1 - vinted_fee), 2)}


# --------------------------------------------------------------------------- #
# eBay details (cached)
# --------------------------------------------------------------------------- #
def _demo_details(listing: dict) -> dict:
    return {"title": listing["title"], "condition_id": "3000", "condition": "Used", "condition_note": "Light wear.",
            "description_html": f"<p>{listing['title']}.</p><p>Fully tested and working. Posted within 1 working day."
                                "</p><ul><li>Smoke-free home</li><li>Carefully packed</li></ul>",
            "category": "Demo category", "pictures": [], "specifics": [("Brand", "Demo"), ("Model", "Example")],
            "price": listing.get("price") or 0, "quantity_available": listing.get("quantity_available") or 1,
            "quantity_sold": listing.get("quantity_sold") or 0}


def listing_details(conn, listing: dict, refresh: bool = False) -> dict:
    lid = listing["legacy_item_id"]
    if _demo():
        return _demo_details(listing)
    row = conn.execute("SELECT data, fetched_at FROM listing_details WHERE legacy_item_id=?", (lid,)).fetchone()
    if row and not refresh:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(row["fetched_at"].replace("Z", "+00:00"))
            if age < DETAILS_MAX_AGE:
                return json.loads(row["data"])
        except ValueError:
            pass
    from ebay_api import EbayClient
    data = EbayClient().get_item_details(lid)
    with write_lock():
        conn.execute("INSERT INTO listing_details(legacy_item_id, data, fetched_at) VALUES (?,?,?) "
                     "ON CONFLICT(legacy_item_id) DO UPDATE SET data=excluded.data, fetched_at=excluded.fetched_at",
                     (lid, json.dumps(data), now_iso()))
        conn.commit()
    return data


def _source_listing(conn, item_id: int):
    """The listing to copy from: the live one if there is one, else the most recent."""
    r = conn.execute("""SELECT * FROM listings WHERE item_id=?
                        ORDER BY (status='active') DESC, start_time DESC LIMIT 1""", (item_id,)).fetchone()
    return dict(r) if r else None


# --------------------------------------------------------------------------- #
# Keep eBay in step with a sale made somewhere else
# --------------------------------------------------------------------------- #
def ebay_stock_for_item(conn, item_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT legacy_item_id, title, quantity_available FROM listings
           WHERE item_id=? AND status='active' ORDER BY start_time""", (item_id,))]


def take_off_ebay(conn, item_id: int, quantity: int, channel: str) -> str:
    """After `quantity` sold on `channel`, lower the eBay listing's stock by that much, ending it if none are left.
    Returns a sentence for the user. Never raises for eBay errors: the sale is already saved."""
    listings = ebay_stock_for_item(conn, item_id)
    if not listings:
        return ""
    if _demo():
        return "Demo mode: on your real account the eBay listing would be updated now."
    from ebay_api import EbayClient, EbayError
    client, done, left_to_take = EbayClient(), [], quantity
    try:
        for L in listings:
            if left_to_take <= 0:
                break
            avail, _ = client.listing_quantity(L["legacy_item_id"])
            take = min(avail, left_to_take)
            left_to_take -= take
            remaining = avail - take
            if remaining <= 0:
                client.end_listing(L["legacy_item_id"])
                status, msg = "ended", f"ended eBay listing #{L['legacy_item_id']}"
            else:
                remaining = client.set_available(L["legacy_item_id"], remaining)
                status, msg = "active", f"eBay listing #{L['legacy_item_id']} now has {remaining} left"
            with write_lock():
                conn.execute("UPDATE listings SET quantity_available=?, status=? WHERE legacy_item_id=?",
                             (max(remaining, 0), status, L["legacy_item_id"]))
                add_event(conn, item_id, "ebay", f"Sold on {channel}: {msg}", L["legacy_item_id"])
                conn.commit()
            done.append(msg)
    except EbayError as e:
        with write_lock():
            notify(conn, "issue", "Couldn't update eBay after a sale elsewhere",
                   f"{e}. Check the listing on eBay so it isn't sold twice.", f"/inventory/{item_id}")
            conn.commit()
        return f"The sale is saved, but eBay couldn't be updated ({e}). Please check the listing on eBay."
    return ("Also " + "; ".join(done) + ".") if done else ""


def vinted_reminder_for_ebay_sale(conn, legacy_id: str, title: str, line_id: str, sold_at: str) -> None:
    """An eBay sale of something also listed on Vinted: remind to take it down there (we can't do it for you)."""
    r = conn.execute("""SELECT i.id, i.vinted_url FROM listings l JOIN items i ON i.id = l.item_id
                        WHERE l.legacy_item_id=? AND i.vinted_listed_at IS NOT NULL""", (legacy_id,)).fetchone()
    if not r:
        return
    left = conn.execute("""SELECT COALESCE(SUM(quantity_available), 0) FROM listings
                           WHERE item_id=? AND status='active'""", (r["id"],)).fetchone()[0]
    body = ("It's also listed on Vinted. " + ("Nothing is left on eBay, so delete or mark it sold on Vinted."
                                              if not left else f"{left} still on eBay: check the quantity on Vinted."))
    notify(conn, "issue", f"Take it off Vinted: {title}", body, f"/inventory/{r['id']}/vinted",
           key=f"vinted-{line_id}", at=sold_at)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
def register(app):
    @app.route("/inventory/<int:item_id>/vinted", methods=["GET", "POST"])
    @login_required
    def vinted_copy(item_id):
        conn = connect()
        try:
            item = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if not item:
                abort(404)
            if request.method == "POST":
                act = request.form.get("action")
                with write_lock():
                    if act == "listed":
                        url = (request.form.get("vinted_url") or "").strip()
                        u = urlparse(url)
                        if url and (u.scheme != "https" or not re.search(r"(^|\.)vinted\.[a-z.]+$", u.hostname or "")):
                            url = ""                     # only keep real https Vinted links
                        conn.execute("UPDATE items SET vinted_listed_at=?, vinted_url=? WHERE id=?",
                                     (item["vinted_listed_at"] or now_iso(), url or None, item_id))
                        add_event(conn, item_id, "vinted", "Listed on Vinted" + (f": {url}" if url else ""))
                        flash("Marked as listed on Vinted. If it sells on eBay you'll get a reminder to take it "
                              "down there.", "ok")
                    elif act == "unlisted":
                        conn.execute("UPDATE items SET vinted_listed_at=NULL, vinted_url=NULL WHERE id=?", (item_id,))
                        add_event(conn, item_id, "vinted", "No longer on Vinted")
                        flash("No longer marked as on Vinted.", "ok")
                    elif act == "fee":
                        Settings().set("vinted_fee_pct", parse_float(request.form.get("vinted_fee_pct")) or 0)
                    conn.commit()
                return redirect(url_for("vinted_copy", item_id=item_id))
            listing = _source_listing(conn, item_id)
            details, error = None, None
            if listing:
                try:
                    details = listing_details(conn, listing, refresh=bool(request.args.get("refresh")))
                except Exception as e:  # noqa: BLE001 - show the page anyway, with what we have
                    error = str(e)
            title = (details or {}).get("title") or item["title"]
            price = (details or {}).get("price") or (listing or {}).get("price") or 0
            ctx = dict(
                item=dict(item), listing=listing, details=details, error=error, title=title,
                description=vinted_description(details) if details else "",
                condition=_COND.get(str((details or {}).get("condition_id")), ""),
                conditions=VINTED_CONDITIONS, pictures=(details or {}).get("pictures", []),
                price=price_suggestion(conn, float(price)) if price else None,
                specifics=(details or {}).get("specifics", []))
        finally:
            conn.close()
        return render_template("vinted.html", **ctx)

    @app.route("/inventory/<int:item_id>/photos.zip")
    @login_required
    def item_photos_zip(item_id):
        conn = connect()
        try:
            listing = _source_listing(conn, item_id)
            if not listing:
                abort(404)
            details = listing_details(conn, listing)
        finally:
            conn.close()
        buf, n = io.BytesIO(), 0
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
            for i, url in enumerate(details.get("pictures", [])[:20], 1):
                host = urlparse(url).hostname or ""
                if urlparse(url).scheme != "https" or not host.endswith(EBAY_IMAGE_HOSTS):
                    continue                                    # only eBay's own image servers
                # ask for the largest size eBay keeps (s-l1600) instead of the listing thumbnail size
                url = re.sub(r"/s-l\d+\.", "/s-l1600.", url)
                try:
                    r = requests.get(url, timeout=20)
                    r.raise_for_status()
                except requests.RequestException:
                    continue
                ext = ".png" if "png" in r.headers.get("Content-Type", "") else ".jpg"
                z.writestr(f"photo-{i:02d}{ext}", r.content)
                n += 1
        if not n:
            flash("Couldn't download the photos from eBay. Try again, or save them from the listing.", "warn")
            return redirect(url_for("vinted_copy", item_id=item_id))
        name = re.sub(r"[^A-Za-z0-9]+", "-", listing["title"]).strip("-")[:50] or "photos"
        return Response(buf.getvalue(), mimetype="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}.zip"'})
