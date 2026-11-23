"""Other shops: listing text, eBay stock updates, CardTrader/Shopify order import and stock kept in step."""
import re

import pytest

import crosslist
import db
import ebay_api
import fx
import stores


def test_html_to_text():
    t = crosslist.html_to_text("<ul><li>One</li><li>Two</li></ul><p>Hi&nbsp;there<br>line</p><script>x()</script>")
    assert t == "• One\n• Two\n\nHi there\nline"


@pytest.mark.parametrize("title,expect", [("Dark Magician SDY-006 Ultra Rare", ("SDY", "006")),
                                          ("Blue-Eyes LOB-EN001 1st", ("LOB", "001")),
                                          ("Pot of Greed", (None, None)), ("RA01-EN054 Ash", ("RA01", "054"))])
def test_set_codes(title, expect):
    assert stores.guess_set_code(title) == expect


def test_blueprint_ranking():
    bps = [{"id": 1, "name": "Dark Magician"}, {"id": 2, "name": "Dark Magic Curtain"}, {"id": 3, "name": "Magician of Faith"}]
    assert stores.rank_blueprints(bps, "Dark Magician SDY-006 Ultra Rare")[0]["id"] == 1


class FakeEbay:
    """eBay's quantity revision, either as 'available' or as 'available + sold'."""
    mode, store, ended = "available", {}, []

    def __init__(self, *a, **k): pass

    def listing_quantity(self, lid):
        q, sold = FakeEbay.store[lid]
        return max(q - sold, 0), sold

    def trading(self, call, xml):
        lid = re.search(r"<ItemID>(.*?)</ItemID>", xml).group(1)
        q = int(re.search(r"<Quantity>(\d+)", xml).group(1))
        _, sold = FakeEbay.store[lid]
        FakeEbay.store[lid] = (q + sold if FakeEbay.mode == "available" else q, sold)

    set_available = ebay_api.EbayClient.set_available

    def end_listing(self, lid):
        FakeEbay.ended.append(lid)


@pytest.fixture()
def live(flask_app, conn, monkeypatch):
    monkeypatch.setattr(ebay_api, "EbayClient", FakeEbay)
    flask_app.config["DEMO"] = False
    r = conn.execute("SELECT legacy_item_id, item_id FROM listings WHERE status='active' AND item_id IS NOT NULL").fetchone()
    conn.execute("UPDATE listings SET status='ended' WHERE item_id=? AND legacy_item_id<>?", (r["item_id"], r["legacy_item_id"]))
    conn.commit()
    with flask_app.test_request_context():
        yield conn, r["legacy_item_id"], r["item_id"]


@pytest.mark.parametrize("mode", ["available", "total"])
def test_take_off_ebay_lowers_quantity_either_way(live, mode):
    conn, lid, item = live
    FakeEbay.mode, FakeEbay.store[lid] = mode, (7, 2)          # 5 available, 2 sold
    msg = crosslist.take_off_ebay(conn, item, 2, "Vinted")
    assert "3 left" in msg and FakeEbay().listing_quantity(lid) == (3, 2)


def test_take_off_ebay_ends_the_last_one(live):
    conn, lid, item = live
    FakeEbay.store[lid], FakeEbay.ended = (3, 2), []
    assert "ended" in crosslist.take_off_ebay(conn, item, 1, "Vinted") and FakeEbay.ended == [lid]


class FakeCT:
    calls = []

    def increment(self, pid, d): FakeCT.calls.append((pid, d))

    def iter_orders(self, since):
        yield {"id": 9, "code": "AB12", "state": "paid", "paid_at": db.now_iso(),
               "seller_subtotal": {"cents": 1000, "currency": "EUR"}, "seller_total": {"cents": 1150, "currency": "EUR"},
               "seller_fee_amount": {"cents": 50, "currency": "EUR"},
               "order_items": [{"id": 77, "product_id": 555, "name": "Dark Magician", "quantity": 1,
                                "seller_price": {"cents": 1000, "currency": "EUR"}}]}


class FakeShopify:
    calls = []

    def adjust(self, inv, loc, d):
        FakeShopify.calls.append((inv, d))
        return 2 + d

    def iter_orders(self, since):
        yield {"id": "gid://shopify/Order/1", "name": "#1001", "processedAt": db.now_iso(), "cancelledAt": None,
               "displayFinancialStatus": "PAID", "totalShippingPriceSet": {"shopMoney": {"amount": "3.00", "currencyCode": "GBP"}},
               "shippingAddress": {"countryCodeV2": "GB", "city": "Glasgow"},
               "lineItems": {"nodes": [{"id": "gid://shopify/LineItem/5", "title": "Dark Magician", "quantity": 1,
                                        "discountedTotalSet": {"shopMoney": {"amount": "12.00", "currencyCode": "GBP"}},
                                        "product": {"id": "gid://shopify/Product/42"}}]}}


@pytest.fixture()
def shops(live, monkeypatch):
    conn, lid, item = live
    monkeypatch.setattr(fx, "rates", lambda: {"EUR": 1.0, "GBP": 0.85})
    monkeypatch.setattr(stores, "ct_client", lambda: FakeCT())
    monkeypatch.setattr(stores, "shopify_client", lambda: FakeShopify())
    monkeypatch.setattr(crosslist, "take_off_ebay", lambda *a, **k: "")
    FakeCT.calls, FakeShopify.calls = [], []
    db.Settings().set_many({"shopify_location_id": "gid://shopify/Location/1", "shopify_fee_pct": "2",
                            "shopify_fee_fixed": "0.25"})
    for ch, ext, inv in [("CardTrader", "555", None), ("Shopify", "gid://shopify/Product/42", "gid://shopify/InventoryItem/8")]:
        conn.execute("INSERT INTO channel_listings(item_id, channel, external_id, inventory_item_id, quantity, status, "
                     "created_at) VALUES (?,?,?,?,2,'active','2000-01-01')", (item, ch, ext, inv))
    conn.commit()
    return conn, item


def test_orders_import_once_and_convert_currency(shops):
    conn, item = shops
    assert stores.import_cardtrader(conn, FakeCT()) == 1 and stores.import_cardtrader(conn, FakeCT()) == 0
    assert stores.import_shopify(conn, FakeShopify()) == 1
    ct = conn.execute("SELECT * FROM sales WHERE line_item_id='CT-77'").fetchone()
    assert ct["item_id"] == item and ct["item_price"] == pytest.approx(8.5) and ct["manual_fees"] == pytest.approx(0.42)
    sh = conn.execute("SELECT * FROM sales WHERE line_item_id='SH-5'").fetchone()
    assert sh["delivery_charged"] == pytest.approx(3.0) and sh["manual_fees"] == pytest.approx(15 * 0.02 + 0.25, abs=0.01)


def test_each_sale_takes_stock_off_the_other_shops_once(shops):
    conn, item = shops
    stores.import_cardtrader(conn, FakeCT())
    stores.import_shopify(conn, FakeShopify())
    stores.propagate_recent_sales(conn)
    assert ("gid://shopify/InventoryItem/8", -1) in FakeShopify.calls      # CardTrader sale -> Shopify
    assert ("555", -1) in FakeCT.calls                                      # Shopify sale -> CardTrader
    before = (len(FakeCT.calls), len(FakeShopify.calls))
    assert stores.propagate_recent_sales(conn) == 0
    assert (len(FakeCT.calls), len(FakeShopify.calls)) == before


def test_sales_before_listing_dont_count(shops):
    conn, item = shops
    conn.execute("UPDATE channel_listings SET created_at=? WHERE item_id=?", ("2999-01-01", item))
    conn.commit()
    stores.import_cardtrader(conn, FakeCT())
    stores.propagate_recent_sales(conn)
    assert FakeShopify.calls == []
